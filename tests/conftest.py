import shutil
import hashlib
import json
import yaml
from datetime import date, timedelta

from jobhunt.profile_store import Profile
from pathlib import Path

import pytest

from jobhunt.config import Ctx

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline_requests(monkeypatch, request):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must mock HTTP requests; live network access is disabled")

    monkeypatch.setattr("requests.sessions.Session.request", blocked)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for key in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'TELEGRAM_USER_ID', 'OPENAI_API_KEY', 'CODEX_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    # Legacy unit tests isolate availability at the external boundary.
    if request.module.__name__.endswith(('test_smoke', 'test_resilience')):
        from jobhunt.availability import Check
        monkeypatch.setattr('jobhunt.availability.inspect', lambda job: Check('open', {'fixture': True}))


@pytest.fixture
def ctx(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    for name in ("settings.yaml", "titles.yaml", "dealbreakers.yaml"):
        shutil.copyfile(ROOT / "config" / name, config / name)
    for sample in (ROOT / "config" / "examples").iterdir():
        if sample.is_file():
            shutil.copyfile(sample, config / sample.name)
    context = Ctx(tmp_path)
    # Explicit fictional model identifiers for mocked legacy client regression tests.
    context.settings['llm'].update(models=['gemini-3.8-flash', 'gemini-3.6-flash'], rpm=8, attempts_per_model=2)
    return context


def confirm_profile(ctx):
    ctx.profile['example_profile'] = False
    w, prefs = ctx.profile['work_authorization'], ctx.profile['preferences']
    w.update(authorized_to_work_us_now='Yes', earliest_start_date='January 2027',
             answers_confirmed_on=date.today().isoformat(), answers_valid_until=(date.today() + timedelta(days=30)).isoformat())
    prefs.update(salary_answer_text='Open', willing_to_relocate=True)
    (ctx.root / 'config' / 'profile.yaml').write_text(yaml.safe_dump(ctx.profile))
    ctx.db.kv_set('reviewed_profile_hash', Profile(ctx).fingerprint())


def prepare_job(ctx, jid):
    profile = Profile(ctx)
    path = ctx.out_dir / f'{jid}.pdf'
    path.write_bytes(b'%PDF-1.4 fictional artifact for unit boundary tests')
    ctx.db.x('INSERT INTO artifacts(job_id,kind,path,sha256,profile_hash,validation_json) VALUES(?,?,?,?,?,?)',
             (jid, 'resume', str(path), hashlib.sha256(path.read_bytes()).hexdigest(), profile.fingerprint(), '{"valid":true}'))
    ctx.db.set_job(jid, status='tailored', fit_final=8, resume_path=str(path), documents_valid=1,
                   document_profile_hash=profile.fingerprint())
    return jid
