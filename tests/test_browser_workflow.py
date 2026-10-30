"""Actual Chromium exercises a loopback-only employer form; never a real ATS."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import confirm_profile, prepare_job
from jobhunt import apply as applications
from jobhunt import availability, tailor
from jobhunt.apply import hitl
from jobhunt.apply.browser import PlaywrightBackend
from jobhunt.profile_store import Profile
from jobhunt.llm import FakeLLM

pytestmark = pytest.mark.browser

FORM = '''<!doctype html><html><body><h1>Software Engineer</h1>
<form id="application_form" method="post" action="/submit" enctype="multipart/form-data">
<label for="first">First Name</label><input id="first" name="first_name" required>
<label for="last">Last Name</label><input id="last" name="last_name" required>
<label for="email">Email</label><input id="email" name="email" type="email" required>
<label for="resume">Resume</label><input id="resume" name="resume" type="file" required>
EXTRA
<button type="submit">Submit Application</button></form></body></html>'''


@pytest.fixture
def employer():
    pytest.importorskip('playwright.sync_api')
    if not os.environ.get('JOBHUNT_BROWSER_TESTS'):
        pytest.skip('Set JOBHUNT_BROWSER_TESTS=1 after installing Chromium for local browser acceptance tests')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(self.server.html.encode())
        def do_POST(self):
            self.server.submissions += 1
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(self.server.receipt.encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.html = FORM.replace('EXTRA', '')
    server.receipt = '<html><body><div id="application_confirmation">Application received. Thank you for applying.</div></body></html>'
    server.submissions = 0
    server.url = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class Notifier:
    def enqueue(self, *a, **k): pass
    def send(self, *a, **k): pass


@pytest.fixture
def prepared(ctx, employer):
    confirm_profile(ctx)
    url = employer.url + '/job'
    ctx.db.upsert_job({'source': 'test', 'company': 'Example', 'title': 'Software Engineer', 'location': 'New York, NY',
                      'url': url, 'apply_url': url, 'description': 'Python API role'})
    job = ctx.db.one('SELECT * FROM jobs')
    prepare_job(ctx, job['id'])
    ctx.settings['apply']['browser_timeout_ms'] = 1500
    ctx.db.kv_set('dry_runs_ok', 20)
    ctx.db.kv_set('live_enabled', 1)
    return job, PlaywrightBackend(ctx.settings, ctx, Profile(ctx), fixture_url=employer.url)


OPEN = lambda job: availability.Check('open', {'local_fixture': True})


def run(ctx, backend, dry=False):
    return applications.run(ctx, Profile(ctx), Notifier(), backend=backend, force_dry=dry, checker=OPEN)


def test_browser_dry_run_blocks_all_submission_requests(ctx, employer, prepared):
    job, backend = prepared
    employer.html = FORM.replace('EXTRA', '<script>document.querySelector("#first").addEventListener("input",()=>fetch("/submit",{method:"POST"}).catch(()=>{}))</script>')
    result = run(ctx, backend, dry=True)
    assert result['dry_ok'] == 1, ctx.db.q('SELECT * FROM applications')
    assert employer.submissions == 0
    assert ctx.db.one('SELECT submit_started_at FROM applications')['submit_started_at'] is None


def test_browser_explicit_approval_and_confirmation(ctx, employer, prepared):
    job, backend = prepared
    first = run(ctx, backend)
    assert first['needs_human'] == 1 and employer.submissions == 0
    req = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    assert req['kind'] == 'approval'
    assert hitl.resolve(ctx.db, 'approve', req['id'], profile_hash=Profile(ctx).fingerprint())
    second = run(ctx, backend)
    assert second['submitted'] == 1, ctx.db.q('SELECT status,detail FROM applications')
    assert employer.submissions == 1
    evidence = json.loads(ctx.db.one("SELECT evidence_json FROM applications WHERE status='SUBMITTED'")['evidence_json'])
    assert Path(evidence['confirmation']['screenshot']).is_file()
    assert evidence['confirmation']['individually_approved']


def test_browser_missing_receipt_requires_reconciliation(ctx, employer, prepared):
    job, backend = prepared
    run(ctx, backend)
    req = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    hitl.resolve(ctx.db, 'approve', req['id'], profile_hash=Profile(ctx).fingerprint())
    employer.receipt = '<html><body>Unexpected response</body></html>'
    result = run(ctx, backend)
    assert result['submitted'] == 0 and employer.submissions == 1
    assert ctx.db.one("SELECT COUNT(*) n FROM applications WHERE status='SUBMISSION_UNKNOWN'")['n'] == 1
    run(ctx, backend)
    assert employer.submissions == 1


def test_browser_unknown_question_and_changed_certification(ctx, employer, prepared):
    job, backend = prepared
    checkbox = '<label for="cert">I certify my application is accurate.</label><input id="cert" type="checkbox" name="cert" required>'
    employer.html = FORM.replace('EXTRA', checkbox)
    result = run(ctx, backend)
    req = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    assert req['kind'] == 'attestation' and result['needs_human'] == 1
    assert hitl.resolve(ctx.db, 'answer', req['id'], 'Yes', profile_hash=Profile(ctx).fingerprint())
    employer.html = employer.html.replace('is accurate.', 'is accurate and I consent to a background check.')
    run(ctx, backend)
    req = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    assert 'background check' in req['question'] and employer.submissions == 0


def test_browser_required_cover_letter_returns_to_tailoring(ctx, employer, prepared):
    job, backend = prepared
    employer.html = FORM.replace('EXTRA', '<label for="cover">Cover Letter</label><input id="cover" name="cover" type="file" required>')
    result = run(ctx, backend)
    current = ctx.db.one('SELECT * FROM jobs')
    assert result['requeued'] == 1 and current['status'] == 'queued' and current['cover_required']
    assert employer.submissions == 0


def test_browser_takeover_checkpoint_and_session_reuse(ctx, employer, prepared):
    job, backend = prepared
    employer.html = '<html><body><label for="password">Password</label><input id="password" type="password"></body></html>'
    run(ctx, backend)
    req = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    assert req['kind'] == 'login'
    folder = ctx.data_dir / 'browser' / job['id']
    assert (folder / 'checkpoint.json').is_file() and (folder / 'session').is_dir()
    # Simulate the user completing the account wall in the same persisted session.
    employer.html = FORM.replace('EXTRA', '')
    assert hitl.resolve(ctx.db, 'resume', req['id'])
    run(ctx, backend)
    assert ctx.db.one("SELECT kind FROM human_requests WHERE status='open'")['kind'] == 'approval'
    assert employer.submissions == 0


def test_browser_revalidates_immediately_before_submit(ctx, employer, prepared):
    job, backend = prepared
    run(ctx, backend)
    req = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    hitl.resolve(ctx.db, 'approve', req['id'], profile_hash=Profile(ctx).fingerprint())
    checks = []
    def checker(job):
        checks.append(job['id'])
        return OPEN(job) if len(checks) == 1 else availability.Check('closed', {'fixture': True})
    result = applications.run(ctx, Profile(ctx), Notifier(), backend=backend, force_dry=False, checker=checker)
    assert len(checks) == 2 and result['submitted'] == 0 and employer.submissions == 0
    assert ctx.db.one('SELECT status FROM jobs')['status'] == 'closed'


def test_real_pdf_complete_text_and_overflow_detection(ctx, employer, tmp_path):
    profile = Profile(ctx)
    plan, _ = tailor.build_plan(ctx, FakeLLM(lambda *_: {}), profile,
                                {'title': 'Software Engineer', 'company': 'Example', 'description': 'Python'})
    html = tailor.render_html(profile, plan)
    pdf = tmp_path / 'resume.pdf'
    assert tailor.html_to_pdf(html, pdf)
    assert tailor.validate_pdf(pdf, html)['valid']
    overflow = html.replace('</body>', '<div style="width:2000px;white-space:nowrap">Overflow</div></body>')
    assert not tailor.html_to_pdf(overflow, tmp_path / 'overflow.pdf')


def test_browser_changed_form_invalidates_application_approval(ctx, employer, prepared):
    job, backend = prepared
    run(ctx, backend)
    request = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    hitl.resolve(ctx.db, 'approve', request['id'], profile_hash=Profile(ctx).fingerprint())
    employer.html = FORM.replace('EXTRA', '<p>New terms: this application now includes a background-check consent.</p>')
    run(ctx, backend)
    assert employer.submissions == 0
    assert ctx.db.one("SELECT COUNT(*) n FROM human_requests WHERE status='open' AND kind='approval'")['n'] == 1


def test_browser_promoted_workflow_still_holds_unknown_shapes(ctx, employer, prepared):
    job, backend = prepared
    run(ctx, backend)
    request = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    hitl.resolve(ctx.db, 'approve', request['id'], profile_hash=Profile(ctx).fingerprint())
    assert run(ctx, backend)['submitted'] == 1
    ctx.db.x("INSERT INTO workflow_approvals VALUES('greenhouse-classic-v1',1,'2026-01-01','Fixture operator promotion')")
    # A second legitimate requisition uses the previously reviewed form shape.
    ctx.db.upsert_job({'source': 'test', 'company': 'Second Employer', 'title': 'Software Engineer',
                      'url': employer.url + '/second', 'apply_url': employer.url + '/second', 'description': 'Python role'})
    second = ctx.db.one("SELECT * FROM jobs WHERE company='Second Employer'")
    prepare_job(ctx, second['id'])
    assert run(ctx, backend)['submitted'] == 1
    assert employer.submissions == 2
    ctx.db.upsert_job({'source': 'test', 'company': 'Third Employer', 'title': 'Software Engineer',
                      'url': employer.url + '/third', 'apply_url': employer.url + '/third', 'description': 'Python role'})
    third = ctx.db.one("SELECT * FROM jobs WHERE company='Third Employer'")
    prepare_job(ctx, third['id'])
    employer.html = FORM.replace('EXTRA', '<label for="portfolio">Portfolio</label><input id="portfolio" name="portfolio">')
    assert run(ctx, backend)['needs_human'] == 1
    assert employer.submissions == 2


def test_browser_pending_authorization_asks_instead_of_inferring(ctx, employer, prepared):
    import yaml
    job, backend = prepared
    ctx.profile['work_authorization'].update(authorized_to_work_us_now=None, earliest_start_date=None,
                                              answers_confirmed_on=None, answers_valid_until=None)
    (ctx.root / 'config' / 'profile.yaml').write_text(yaml.safe_dump(ctx.profile))
    ctx.db.kv_set('reviewed_profile_hash', Profile(ctx).fingerprint())
    prepare_job(ctx, job['id'])
    employer.html = FORM.replace('EXTRA', '<label for="auth">Are you authorized to work in the US?</label><select id="auth" name="auth" required><option value="">Choose</option><option>Yes</option><option>No</option></select>')
    result = run(ctx, backend)
    request = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    assert result['needs_human'] == 1 and request['kind'] == 'question'
    assert 'authorized' in request['question'] and employer.submissions == 0


def test_browser_profile_edit_during_preparation_blocks_submit(ctx, employer, prepared):
    import yaml
    job, backend = prepared
    run(ctx, backend)
    request = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    hitl.resolve(ctx.db, 'approve', request['id'], profile_hash=Profile(ctx).fingerprint())
    checks = []
    def checker(job):
        checks.append(job['id'])
        if len(checks) == 2:
            changed = {**ctx.profile, 'candidate': {**ctx.profile['candidate'], 'name': 'Edited Fictional Name'}}
            (ctx.root / 'config' / 'profile.yaml').write_text(yaml.safe_dump(changed))
        return OPEN(job)
    result = applications.run(ctx, Profile(ctx), Notifier(), backend=backend, force_dry=False, checker=checker)
    assert result['submitted'] == 0 and employer.submissions == 0
    assert ctx.db.one("SELECT kind FROM human_requests WHERE status='open'")['kind'] == 'issue'


def test_browser_existing_receipt_is_not_new_submission_proof(ctx, employer, prepared):
    job, backend = prepared
    employer.html = FORM.replace('EXTRA', '<div id="application_confirmation">Application received</div>')
    run(ctx, backend)
    request = ctx.db.one("SELECT * FROM human_requests WHERE status='open'")
    hitl.resolve(ctx.db, 'approve', request['id'], profile_hash=Profile(ctx).fingerprint())
    result = run(ctx, backend)
    assert result['submitted'] == 0 and employer.submissions == 0
    assert ctx.db.one("SELECT kind FROM human_requests WHERE status='open'")['kind'] == 'interrupted'
