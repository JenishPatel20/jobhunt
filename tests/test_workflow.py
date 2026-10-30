"""Regression tests for ledger integrity, safe boundaries, recovery, and cost controls."""
import hashlib
import json
import sqlite3
import subprocess
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from conftest import confirm_profile, prepare_job
from jobhunt import availability, filter as filt, tailor, track
from jobhunt import apply as applications
from jobhunt.apply import hitl
from jobhunt.apply.backends import FakeBackend, Result, ApplyPilotBackend
from jobhunt.db import DB, dumps
from jobhunt.discover import run as discover
from jobhunt.discover import ats
from jobhunt.eligibility import evaluate
from jobhunt.llm import FakeLLM, ConfigurationError, QuotaExceeded, TemporaryUnavailable
from jobhunt.profile_store import Profile
from jobhunt.providers import CodexSubscription, make_provider
from jobhunt.telegram import Telegram
from jobhunt.util import now_iso
from jobhunt.workflows import enable


def listing(url='https://example.com/jobs/1', **extra):
    return {'source': 'greenhouse', 'board_slug': 'example', 'source_job_id': '1001',
            'company': 'Example Corp', 'title': 'Software Engineer', 'location': 'New York, NY',
            'url': url, 'apply_url': url, 'description': 'Build Python REST APIs and PostgreSQL services. ' * 4, **extra}


def add_job(ctx, **extra):
    ctx.db.upsert_job(listing(**extra))
    return ctx.db.one('SELECT * FROM jobs ORDER BY rowid DESC LIMIT 1')


class Notifier:
    def __init__(self): self.messages = []
    def enqueue(self, text, request_id=None): self.messages.append(text)
    def send(self, text, subject=''): self.messages.append(text)


OPEN = lambda job: availability.Check('open', {'fixture': True})


def test_distinct_requisitions_not_merged_and_all_observations_saved(ctx):
    first = add_job(ctx)
    second = add_job(ctx, url='https://example.com/jobs/2', source_job_id='1002')
    assert first['id'] != second['id'] and second['duplicate_of'] == first['id']
    assert not ctx.db.upsert_job({'source': 'broken', 'title': 'Incomplete'})
    assert not ctx.db.upsert_job(listing())
    assert ctx.db.one('SELECT COUNT(*) n FROM observations')['n'] == 4
    assert ctx.db.one('SELECT COUNT(*) n FROM snapshots')['n'] == 2
    assert ctx.db.one("SELECT disposition FROM observations WHERE source='broken'")['disposition'].startswith('invalid')


def test_description_changes_invalidate_artifacts_and_approvals(ctx):
    job = add_job(ctx)
    ctx.db.set_job(job['id'], status='tailored', documents_valid=1)
    ctx.db.x('INSERT INTO application_approvals VALUES(?,?,?)', (job['id'], 'old', now_iso()))
    ctx.db.upsert_job(listing(description='A different Python job.'))
    current = ctx.db.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
    assert current['status'] == 'new' and not current['documents_valid']
    assert not ctx.db.one('SELECT 1 FROM application_approvals')
    assert ctx.db.one('SELECT COUNT(*) n FROM snapshots')['n'] == 2


def test_migration_preserves_existing_ids_and_attempts(tmp_path):
    path = tmp_path / 'legacy.db'
    conn = sqlite3.connect(path)
    conn.executescript('''CREATE TABLE jobs(id TEXT PRIMARY KEY,dedupe_key TEXT UNIQUE,source TEXT,company TEXT,
      company_norm TEXT,title TEXT,location TEXT,url TEXT,apply_url TEXT,description TEXT,posted_at TEXT,
      discovered_at TEXT,status TEXT, reject_reason TEXT,sponsor_tier TEXT,everify INTEGER,jd_sponsor_positive INTEGER,
      fit_raw REAL,boost REAL,fit_final REAL,fit_json TEXT,missing_skills TEXT,resume_path TEXT,cover_path TEXT,applied_at TEXT,notes TEXT);
      CREATE TABLE applications(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,mode TEXT,status TEXT,started_at TEXT,finished_at TEXT,detail TEXT);
      INSERT INTO jobs(id,dedupe_key,source,company,company_norm,title,location,url,apply_url,description,status)
      VALUES('stable','example|engineer|NY','greenhouse','Example','example','Software Engineer','New York, NY','https://example.com/jobs/1','https://example.com/jobs/1','Legacy facts','applied');
      INSERT INTO applications(job_id,mode,status) VALUES('stable','live','SUBMITTED');''')
    conn.close()
    db = DB(path)
    assert not db.upsert_job(listing(description='Legacy facts'))
    assert db.one('SELECT id,status FROM jobs') == {'id': 'stable', 'status': 'applied'}
    assert db.one('SELECT job_id,status FROM applications') == {'job_id': 'stable', 'status': 'SUBMITTED'}
    db.conn.close()
    db = DB(path)
    assert db.one('PRAGMA user_version')['user_version'] == 2
    assert db.one('SELECT COUNT(*) n FROM jobs')['n'] == 1


def test_stale_listing_is_logged_before_rejection(ctx, monkeypatch):
    (ctx.root / 'config' / 'companies.yaml').write_text('companies:\n- {name: Example, ats: greenhouse, slug: example}\n')
    monkeypatch.setattr(ats, 'FETCHERS', {'greenhouse': lambda *_: [listing(posted_at='2000-01-01'), {'source': 'bad'}]})
    monkeypatch.setattr('jobhunt.discover.time.sleep', lambda _: None)
    stats = discover(ctx)
    assert stats['fetched'] == 2 and stats['new'] == 1
    assert ctx.db.one('SELECT reject_reason FROM jobs')['reject_reason'] == 'posting_age'
    assert ctx.db.one('SELECT COUNT(*) n FROM observations')['n'] == 2


@pytest.mark.parametrize('status,state', [(404, 'closed'), (410, 'closed'), (403, 'uncertain'), (429, 'uncertain'), (503, 'uncertain')])
def test_availability_http_states(ctx, monkeypatch, status, state):
    job = add_job(ctx)
    monkeypatch.setattr('jobhunt.availability.requests.get', lambda *a, **k: SimpleNamespace(status_code=status))
    result = availability.check(ctx, job, 'test')
    assert result.state == state
    assert ctx.db.one('SELECT state FROM availability_checks')['state'] == state


def test_availability_200_requires_identity_and_title(ctx, monkeypatch):
    job = add_job(ctx)
    for payload, state in [({}, 'uncertain'), ({'id': 1001, 'title': 'Wrong role'}, 'uncertain'),
                           ({'id': 1001, 'title': job['title']}, 'open')]:
        monkeypatch.setattr('jobhunt.availability.requests.get', lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload))
        assert availability.inspect(job).state == state


def test_ashby_absence_only_closed_for_complete_feed(ctx, monkeypatch):
    job = add_job(ctx, source='ashby', source_job_id='abc', url='https://jobs.ashbyhq.com/example/abc')
    for payload, state in [({'jobs': [], 'nextCursor': 'next'}, 'uncertain'), ({'jobs': []}, 'closed')]:
        monkeypatch.setattr('jobhunt.availability.requests.get', lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload))
        assert availability.inspect(job).state == state


def test_timeout_never_marks_closed(ctx, monkeypatch):
    job = add_job(ctx)
    def timeout(*a, **k): raise requests.ReadTimeout()
    monkeypatch.setattr('jobhunt.availability.requests.get', timeout)
    assert availability.check(ctx, job, 'test').state == 'uncertain'
    assert ctx.db.one('SELECT status FROM jobs')['status'] == 'new'


def test_source_errors_not_empty_boards(ctx, monkeypatch):
    monkeypatch.setattr(ats, '_get', lambda *a, **k: SimpleNamespace(status_code=403))
    with pytest.raises(ats.SourceUnavailable): ats.greenhouse('example')


def test_restrictions_are_contextual_and_silence_is_unknown(ctx):
    rules = filt.Rules(ctx)
    assert evaluate(ctx, listing(description='US citizenship is not required.'), rules)['state'] == 'eligible_or_unknown'
    assert evaluate(ctx, listing(description='Security clearance preferred.'), rules)['state'] == 'review'
    assert evaluate(ctx, listing(description='Must be a US citizen.'), rules)['state'] == 'incompatible'
    assert evaluate(ctx, listing(description='Build APIs.'), rules)['sponsorship'] == 'unknown'
    assert evaluate(ctx, listing(description='No visa sponsorship is available.'), rules)['state'] == 'incompatible'


def test_pending_authorization_never_inferred_and_changes_expire_answers(ctx):
    job = add_job(ctx)
    profile = Profile(ctx)
    assert profile.answer('Are you authorized to work in the US?', job)[0] is None
    confirm_profile(ctx)
    assert profile.answer('Are you authorized to work in the US?', job)[0] == 'Yes'
    ctx.profile['work_authorization']['answers_valid_until'] = '2000-01-01'
    assert profile.answer('Are you authorized to work in the US?', job)[0] is None
    assert not profile.reviewed()


def test_company_specific_answers_and_exact_attestations(ctx):
    confirm_profile(ctx)
    first = add_job(ctx)
    second = add_job(ctx, url='https://example.com/jobs/2', source_job_id='1002', company='Another')
    profile, notifier = Profile(ctx), Notifier()
    rid = hitl.open_request(ctx.db, notifier, first, 'question', 'Why this company?', {'profile_hash': profile.fingerprint()})
    assert hitl.resolve(ctx.db, 'answer', rid, 'Because of the product.', profile_hash=profile.fingerprint())
    assert profile.answer('Why this company?', first)[0] == 'Because of the product.'
    assert profile.answer('Why this company?', second)[0] is None
    rid = hitl.open_request(ctx.db, notifier, first, 'attestation', 'I certify this application is accurate.', {'profile_hash': profile.fingerprint()})
    assert hitl.resolve(ctx.db, 'answer', rid, 'Yes', profile_hash=profile.fingerprint())
    assert profile.answer('I certify this application is accurate.', first)[0] == 'Yes'
    assert profile.answer('I certify this application is accurate and consent to a background check.', first)[0] is None


def test_tailoring_rejects_foreign_owner_and_source_technology(ctx):
    profile = Profile(ctx)
    response = {'experience': [{'id': 'example-api', 'bullets': [{'src': 'ml-1', 'text': profile.bullets()['ml-1']}]}], 'projects': [], 'skills': {}}
    plan, notes = tailor.build_plan(ctx, FakeLLM(lambda *_: response), profile, listing())
    api = next(item for item in plan['experience'] if item['id'] == 'example-api')
    assert all(b['src'].startswith('api-') for b in api['bullets'])
    assert notes


def test_no_optional_letter_and_pdf_failure_blocks(ctx, monkeypatch):
    job = add_job(ctx)
    ctx.db.set_job(job['id'], status='queued', fit_final=9)
    llm = FakeLLM(lambda *_: {'experience': [], 'projects': [], 'skills': {}})
    monkeypatch.setattr(tailor, 'html_to_pdf', lambda *_: False)
    monkeypatch.setattr(tailor, 'cover_letter', lambda *_: pytest.fail('Optional cover letter generated'))
    result = tailor.run(ctx, llm, Profile(ctx), checker=OPEN)
    assert result['blocked'] == 1
    assert ctx.db.one('SELECT status FROM jobs')['status'] == 'document_blocked'


def test_required_cover_letter_failure_blocks(ctx, monkeypatch):
    job = add_job(ctx)
    ctx.db.set_job(job['id'], status='queued', cover_required=1)
    def pdf(html, path): path.write_bytes(b'pdf'); return True
    monkeypatch.setattr(tailor, 'html_to_pdf', pdf)
    monkeypatch.setattr(tailor, 'cover_letter', lambda *_: ('', ['rejected unsupported claim']))
    tailor.run(ctx, FakeLLM(lambda *_: {}), Profile(ctx), checker=OPEN)
    assert ctx.db.one('SELECT status,cover_path FROM jobs') == {'status': 'document_blocked', 'cover_path': None}


def test_application_claim_is_atomic_and_recoverable(ctx):
    job = add_job(ctx)
    ctx.db.set_job(job['id'], status='tailored')
    assert ctx.db.claim(job['id'], 'live', 'test', {})
    assert ctx.db.claim(job['id'], 'live', 'test', {}) is None
    notifier = Notifier()
    assert applications.recover(ctx, notifier) == 1
    assert applications.recover(ctx, notifier) == 0
    rid = ctx.db.one('SELECT id FROM human_requests')['id']
    assert not hitl.resolve(ctx.db, 'retry', rid)
    assert hitl.resolve(ctx.db, 'retry', rid, 'Checked employer account: no application exists.')
    assert ctx.db.one('SELECT status FROM applications')['status'] == 'RECONCILED_NOT_SUBMITTED'


def test_unknown_submission_is_not_retried_and_exit_code_is_not_proof(ctx):
    confirm_profile(ctx)
    job = add_job(ctx)
    prepare_job(ctx, job['id'])
    ctx.db.kv_set('dry_runs_ok', 20)
    ctx.db.kv_set('live_enabled', 1)
    backend = FakeBackend([Result('SUBMITTED', 'no evidence')])
    result = applications.run(ctx, Profile(ctx), Notifier(), backend=backend, checker=OPEN)
    assert result['submitted'] == 0
    assert ctx.db.one('SELECT status FROM applications')['status'] == 'SUBMISSION_UNKNOWN'
    applications.run(ctx, Profile(ctx), Notifier(), backend=backend, checker=OPEN)
    assert len(backend.calls) == 1
    assert ApplyPilotBackend({'apply': {'applypilot_cmd': [], 'applypilot_dry_flag': ''}}).apply({}, {}, False).status == 'FAILED'


def test_application_rejects_modified_document(ctx):
    confirm_profile(ctx)
    job = add_job(ctx)
    prepare_job(ctx, job['id'])
    path = ctx.db.one('SELECT resume_path FROM jobs')['resume_path']
    Path(path).write_bytes(b'tampered')
    backend = FakeBackend()
    result = applications.run(ctx, Profile(ctx), Notifier(), backend=backend, checker=OPEN)
    assert not backend.calls and result['requeued'] == 1


def test_manual_receipt_remains_in_totals_after_interview(ctx):
    job = add_job(ctx)
    rid = hitl.open_request(ctx.db, Notifier(), job, 'login', 'Apply manually')
    assert not hitl.resolve(ctx.db, 'done', rid)
    assert hitl.resolve(ctx.db, 'done', rid, 'Employer receipt ABC123')
    ctx.db.set_job(job['id'], status='interviewing')
    assert 'Confirmed submitted total: 1' in track.digest(ctx.db)
    path = ctx.root / 'all.csv'
    add_job(ctx, source_job_id='other', url='https://example.com/jobs/2')
    assert track.export_csv(ctx.db, path) == 2


def test_email_matching_holds_ambiguous_roles_and_ignores_late_events(ctx):
    first = add_job(ctx)
    second = add_job(ctx, source_job_id='1002', url='https://example.com/jobs/2', title='Data Engineer')
    for job in (first, second): ctx.db.set_job(job['id'], status='applied', applied_at=now_iso())
    message = {'id': 'm1', 'from': 'jobs@example.com', 'subject': 'Interview with Example', 'snippet': '', 'date': now_iso()}
    track.ingest(ctx.db, [message])
    assert ctx.db.one('SELECT job_id FROM events')['job_id'] is None
    assert ctx.db.one('SELECT COUNT(*) n FROM email_reviews')['n'] == 1
    message.update(id='m2', subject='Interview: Software Engineer at Example')
    track.ingest(ctx.db, [message])
    assert ctx.db.one('SELECT status FROM jobs WHERE id=?', (first['id'],))['status'] == 'interviewing'
    message.update(id='m3', subject='Software Engineer at Example', snippet='Unfortunately not moving forward', date='2000-01-01T00:00:00+00:00')
    track.ingest(ctx.db, [message])
    assert ctx.db.one('SELECT status FROM jobs WHERE id=?', (first['id'],))['status'] == 'interviewing'


def telegram(ctx, monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fictional')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '22')
    monkeypatch.setenv('TELEGRAM_USER_ID', '33')
    return Telegram(ctx.db, Profile(ctx))


def test_telegram_allowlist_duplicate_and_scoped_reply(ctx, monkeypatch):
    confirm_profile(ctx)
    job = add_job(ctx)
    rid = hitl.open_request(ctx.db, Notifier(), job, 'question', 'Why this company?', {'profile_hash': Profile(ctx).fingerprint()})
    bot = telegram(ctx, monkeypatch)
    update = {'update_id': 1, 'message': {'from': {'id': 99}, 'chat': {'id': 22, 'type': 'private'}, 'text': f'/answer {rid} forged'}}
    assert bot.handle(update) == 'unauthorized'
    assert ctx.db.one('SELECT status FROM human_requests')['status'] == 'open'
    update['update_id'] = 2
    update['message']['from']['id'] = 33
    update['message']['text'] = f'/answer {rid} My reviewed answer'
    assert bot.handle(update) == 'resolved'
    assert bot.handle(update) == 'duplicate'
    assert ctx.db.one('SELECT COUNT(*) n FROM scoped_answers')['n'] == 1


def test_telegram_delivery_retry_and_reissue(ctx, monkeypatch):
    job = add_job(ctx)
    rid = hitl.open_request(ctx.db, Notifier(), job, 'login', 'Verification challenge')
    bot = telegram(ctx, monkeypatch)
    bot.reissue()
    def fail(*a, **k): raise requests.ConnectionError('Must never log token-containing URLs')
    monkeypatch.setattr(bot, 'api', fail)
    assert bot.flush() == 0
    row = ctx.db.one('SELECT * FROM outbox WHERE request_id=?', (rid,))
    assert row['state'] == 'pending' and row['attempts'] == 1 and row['last_error'] == 'delivery_failed'
    ctx.db.x('UPDATE outbox SET next_attempt=NULL')
    monkeypatch.setattr(bot, 'api', lambda *a: {'message_id': 1})
    assert bot.flush() == 1
    ctx.db.x("UPDATE human_requests SET last_notified_at='2000-01-01' WHERE id=?", (rid,))
    bot.reissue()
    assert ctx.db.one("SELECT COUNT(*) n FROM outbox WHERE state='pending'")['n'] == 1


def test_provider_refuses_api_authentication_and_no_paid_fallback(ctx, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-propagate')
    calls = []
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        assert 'OPENAI_API_KEY' not in kwargs['env']
        return SimpleNamespace(returncode=0, stdout='Logged in using an API key', stderr='')
    monkeypatch.setattr('jobhunt.providers.subprocess.run', run)
    with pytest.raises(ConfigurationError): CodexSubscription(ctx.db, ctx.settings).text('system', 'prompt')
    assert len(calls) == 1 and ctx.db.llm_calls_today() == 0
    with pytest.raises(ConfigurationError): make_provider(ctx.db, {'llm': {'provider': 'openai_api'}})


def test_provider_limits_and_cooldown(ctx, monkeypatch):
    ctx.settings['llm']['daily_cap'] = 0
    with pytest.raises(QuotaExceeded): CodexSubscription(ctx.db, ctx.settings).text('system', 'prompt')
    ctx.settings['llm']['daily_cap'] = 10
    def run(cmd, **kwargs):
        if cmd[1] == 'login': return SimpleNamespace(returncode=0, stdout='Logged in using ChatGPT', stderr='')
        return SimpleNamespace(returncode=1, stdout='', stderr='quota')
    monkeypatch.setattr('jobhunt.providers.subprocess.run', run)
    provider = CodexSubscription(ctx.db, ctx.settings)
    with pytest.raises(TemporaryUnavailable): provider.text('system', 'prompt')
    assert ctx.db.llm_calls_today() == 1
    with pytest.raises(TemporaryUnavailable): provider.text('system', 'prompt')
    assert ctx.db.llm_calls_today() == 1


def test_workflow_cannot_promote_without_reviewed_confirmations(ctx):
    with pytest.raises(SystemExit): enable(ctx, 'greenhouse-classic-v1', 'reviewed exit codes')


def test_scoring_cache_and_sponsorship_never_rescues_low_fit(ctx):
    job = add_job(ctx)
    ctx.db.x("INSERT INTO sponsors VALUES('example','Example',100,100,'2026','fictional')")
    llm = FakeLLM(lambda *_: {'fit': 6, 'missing_skills': [], 'sponsorship_signal': 'none'})
    filt.run(ctx, llm, Profile(ctx))
    assert ctx.db.one('SELECT status,fit_final FROM jobs') == {'status': 'gap', 'fit_final': 6.0}
    ctx.db.set_job(job['id'], status='new')
    filt.run(ctx, llm, Profile(ctx))
    assert llm.calls == 1


def test_ashby_metadata_preserves_provider_and_compensation(monkeypatch):
    payload = {'jobs': [{'id': 'abc', 'title': 'Data Engineer', 'jobUrl': 'https://jobs.ashbyhq.com/example/abc',
                         'employmentType': 'Intern', 'compensation': {'interval': 'Hour', 'minValue': 40},
                         'secondaryLocations': [{'location': 'Boston, MA'}]}]}
    monkeypatch.setattr(ats, '_get', lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload))
    job = ats.ashby('example')[0]
    assert job['source'] == 'ashby' and job['source_job_id'] == 'abc'
    assert job['employment_type'] == 'Intern' and job['compensation']['interval'] == 'Hour'
    assert job['locations']


def test_duplicate_review_and_scoped_numeric_answer(ctx):
    confirm_profile(ctx)
    first = add_job(ctx)
    second = add_job(ctx, url='https://example.com/jobs/2', source_job_id='1002')
    prepare_job(ctx, second['id'])
    result = applications.run(ctx, Profile(ctx), Notifier(), backend=FakeBackend(), checker=OPEN)
    assert result['needs_human'] == 1
    req = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    assert req['kind'] == 'duplicate'
    assert hitl.resolve(ctx.db, 'resume', req['id'])
    assert ctx.db.one('SELECT duplicate_of FROM jobs WHERE id=?', (second['id'],))['duplicate_of'] is None
    profile = Profile(ctx)
    rid = hitl.open_request(ctx.db, Notifier(), first, 'question', 'Expected salary', {'profile_hash': profile.fingerprint()})
    assert hitl.resolve(ctx.db, 'answer', rid, '100000', profile_hash=profile.fingerprint())
    assert profile.answer('Expected salary', first)[0] == '100000'


def test_workflow_promotion_requires_distinct_individual_trials(ctx):
    ctx.settings['apply']['reviewed_trials_required'] = 2
    for index in range(2):
        job = add_job(ctx, source_job_id=str(index), url=f'https://example.com/jobs/{index}')
        confirmation = {'confirmation': {'individually_approved': True, 'kind': 'employer_page'}}
        ctx.db.x("INSERT INTO applications(job_id,mode,status,workflow,evidence_json) VALUES(?,'live','SUBMITTED','greenhouse-classic-v1',?)", (job['id'], dumps(confirmation)))
    enable(ctx, 'greenhouse-classic-v1', 'Reviewed forms and receipts for both trials')
    assert ctx.db.one('SELECT enabled FROM workflow_approvals')['enabled'] == 1


def test_claimed_browser_failure_notifies_and_can_resume(ctx):
    confirm_profile(ctx)
    job = add_job(ctx)
    prepare_job(ctx, job['id'])
    result = applications.run(ctx, Profile(ctx), Notifier(), backend=FakeBackend([Result('FAILED', 'Browser missing')]), checker=OPEN)
    assert result['failed'] == 1 and result['needs_human'] == 1
    request = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    assert request['kind'] == 'issue'
    assert hitl.resolve(ctx.db, 'resume', request['id'])
    assert ctx.db.one('SELECT status FROM jobs')['status'] == 'tailored'


def test_malformed_fields_and_urls_are_observations_not_pipeline_crashes(ctx):
    for record in (listing(location=['bad vendor type']), listing(url='http://[invalid'), listing(title={'bad': 'type'})):
        assert not ctx.db.upsert_job(record)
    assert ctx.db.one('SELECT COUNT(*) n FROM observations')['n'] == 3
    assert ctx.db.one('SELECT COUNT(*) n FROM jobs')['n'] == 0


def test_one_backend_error_does_not_stop_other_jobs(ctx):
    confirm_profile(ctx)
    for index in range(2):
        job = add_job(ctx, source_job_id=str(index), url=f'https://example.com/jobs/{index}', company=f'Employer {index}')
        prepare_job(ctx, job['id'])
    class Backend:
        calls = 0
        def apply(self, *args):
            self.calls += 1
            if self.calls == 1: raise RuntimeError('unknown interruption')
            return Result('DRY_RUN_READY', 'Prepared')
    result = applications.run(ctx, Profile(ctx), Notifier(), backend=Backend(), checker=OPEN)
    assert result['needs_human'] == 1 and result['dry_ok'] == 1
    assert ctx.db.one("SELECT COUNT(*) n FROM applications WHERE status='SUBMISSION_UNKNOWN'")['n'] == 1


def test_init_does_not_replace_existing_candidate_data(ctx, monkeypatch):
    from jobhunt.cli import main
    path = ctx.root / 'config' / 'profile.yaml'
    original = path.read_bytes()
    monkeypatch.setenv('JOBHUNT_HOME', str(ctx.root))
    main(['init'])
    assert path.read_bytes() == original


def test_version_two_migration_makes_recoverable_backup(tmp_path):
    path = tmp_path / 'legacy.db'
    # A v1 database initialized from the original schema, without tracked personal data.
    from jobhunt.db import SCHEMA
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO kv VALUES('legacy-marker','preserved')")
    connection.commit()
    connection.close()
    DB(path).conn.close()
    backup = sqlite3.connect(tmp_path / 'legacy.db.before-v2.db')
    assert backup.execute("SELECT v FROM kv WHERE k='legacy-marker'").fetchone()[0] == 'preserved'
    backup.close()


def test_profile_validation_and_mandatory_consent_gate(ctx):
    profile = Profile(ctx)
    profile.validate()
    ctx.answer_bank['always_human_patterns'] = []
    ctx.answer_bank['answers'].insert(0, {'name': 'overbroad', 'patterns': ['privacy'], 'answer': 'Yes'})
    assert profile.answer('I accept this privacy policy')[0] is None
    ctx.profile['projects'][0]['bullets'][0]['id'] = 'api-1'
    with pytest.raises(ValueError, match='globally unique'):
        profile.validate()


def test_lever_availability_keeps_requirement_lists(ctx, monkeypatch):
    job = add_job(ctx, source='lever', source_job_id='abc', url='https://jobs.lever.co/example/abc',
                  description='Build Python APIs\nNo sponsorship available.')
    response = {'id': 'abc', 'text': job['title'], 'descriptionPlain': 'Build Python APIs',
                'lists': [{'content': '<p>No sponsorship available.</p>'}]}
    monkeypatch.setattr('jobhunt.availability.requests.get', lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: response))
    check = availability.check(ctx, job, 'test')
    assert check.state == 'open' and 'No sponsorship available.' in check.evidence['description']


def test_malformed_vendor_row_does_not_lose_other_discoveries(ctx, monkeypatch):
    payload = {'jobs': [{'id': 1, 'title': 'Bad', 'location': ['unexpected vendor type']},
                        {'id': 2, 'title': 'Software Engineer', 'location': {'name': 'New York, NY'},
                         'absolute_url': 'https://job-boards.greenhouse.io/example/jobs/2', 'content': '<p>Python</p>'}]}
    monkeypatch.setattr(ats, '_get', lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload))
    rows = ats.greenhouse('example')
    assert len(rows) == 2
    for row in rows: ctx.db.upsert_job(row)
    assert ctx.db.one('SELECT COUNT(*) n FROM observations')['n'] == 2
    assert ctx.db.one('SELECT COUNT(*) n FROM jobs')['n'] == 1
