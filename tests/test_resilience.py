import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
import yaml
from conftest import confirm_profile, prepare_job

from jobhunt import filter as filt
from jobhunt import cli
from jobhunt import apply as apply_mod
from jobhunt.apply import hitl
from jobhunt.config import Ctx
from jobhunt.db import DB
from jobhunt.discover.scrapers import _cell
from jobhunt.llm import FakeLLM, Gemini, ModelUnavailable, TemporaryUnavailable
from jobhunt.profile_store import Profile

def listing(url, description, company="Acme"):
    return dict(source="test", company=company, title="Software Engineer", location="New York, NY",
                url=url, apply_url=url, description=description)


def response(code):
    class Response:
        status_code = code
        headers = {}
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
    return Response()


@pytest.mark.parametrize("first", [404, 429, 503, "timeout"])
def test_gemini_falls_back_and_counts_every_attempt(ctx, monkeypatch, first):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr("jobhunt.llm.time.sleep", lambda _: None)
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        if "3.8" in url:
            if first == "timeout":
                raise requests.ReadTimeout("slow")
            return response(first)
        return response(200)
    monkeypatch.setattr("jobhunt.llm.requests.post", post)
    gemini = Gemini(ctx.db, ctx.settings)
    assert gemini.text("system", "prompt") == "OK"
    expected = 2 if first == 404 else 3
    assert len(calls) == expected
    assert ctx.db.llm_calls_today() == expected
    assert all("temperature" not in call[1]["json"]["generationConfig"] for call in calls)
    assert ctx.db.one("SELECT COUNT(*) n FROM llm_attempts")["n"] == expected


def test_gemini_cooldown_then_recovery(ctx, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr("jobhunt.llm.time.sleep", lambda _: None)
    calls = []
    def unavailable(url, **_):
        calls.append(url)
        return response(503)
    monkeypatch.setattr("jobhunt.llm.requests.post", unavailable)
    gemini = Gemini(ctx.db, ctx.settings)
    with pytest.raises(TemporaryUnavailable):
        gemini.text("system", "prompt")
    assert len(calls) == 4
    assert gemini.cooldown_until() > datetime.now(timezone.utc)
    with pytest.raises(TemporaryUnavailable):
        gemini.text("system", "prompt")
    assert len(calls) == 4
    ctx.db.kv_set("llm_cooldown_until", (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    monkeypatch.setattr("jobhunt.llm.requests.post", lambda *_a, **_k: response(200))
    assert gemini.text("system", "prompt") == "OK"
    assert ctx.db.kv_get("llm_cooldown_until") == ""


def test_successful_fallback_is_preferred_on_next_run(ctx, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    calls = []
    def post(url, **_):
        calls.append(url)
        return response(404 if "3.8" in url else 200)
    monkeypatch.setattr("jobhunt.llm.requests.post", post)
    assert Gemini(ctx.db, ctx.settings).text("system", "prompt") == "OK"
    calls.clear()
    assert Gemini(ctx.db, ctx.settings).text("system", "prompt") == "OK"
    assert len(calls) == 1 and "3.6" in calls[0]


def test_gemini_all_404_keeps_pending(ctx, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr("jobhunt.llm.requests.post", lambda *_a, **_k: response(404))
    ctx.db.upsert_job(listing("https://example.com/1", "Build Python APIs. " * 12))
    result = filt.run(ctx, Gemini(ctx.db, ctx.settings), Profile(ctx), limit=1)
    assert result["model_error"]
    assert ctx.db.one("SELECT status FROM jobs")["status"] == "passed_rules"


def test_nan_cleanup_repairs_saved_row_and_refreshes_description(ctx):
    assert _cell(float("nan")) == ""
    db = ctx.db
    db.upsert_job(listing("https://example.com/1", "nan"))
    jid = db.one("SELECT id FROM jobs")["id"]
    db.set_job(jid, apply_url="nan", status="passed_rules")
    db.conn.close()
    repaired = DB(ctx.data_dir / "jobhunt.db")
    row = repaired.one("SELECT * FROM jobs")
    assert row["id"] == jid and row["status"] == "passed_rules"
    assert row["apply_url"] == "https://example.com/1" and row["description"] is None
    assert repaired.upsert_job(listing("https://example.com/1", "Build Python APIs. " * 12)) is False
    assert len(repaired.one("SELECT description FROM jobs")["description"]) >= 100
    assert repaired.one("SELECT COUNT(*) n FROM jobs")["n"] == 1


def test_complete_first_and_tentative_threshold(ctx):
    db = ctx.db
    db.upsert_job(listing("https://example.com/thin", "", company="Thin"))
    db.upsert_job(listing("https://example.com/full", "Build Python services and APIs with a team. " * 4, company="Full"))
    seen = []
    def score(_system, prompt, _json):
        seen.append(prompt)
        return {"fit": 8, "missing_skills": [], "seniority_flag": False,
                "requires_citizenship_or_clearance": False, "sponsorship_signal": "none", "location_us": True}
    result = filt.run(ctx, FakeLLM(score), Profile(ctx), limit=1)
    assert result["scored"] == 1 and "Company: Full" in seen[0]
    result = filt.run(ctx, FakeLLM(score), Profile(ctx))
    thin = db.one("SELECT * FROM jobs WHERE company='Thin'")
    assert thin["status"] == "gap" and json.loads(thin["fit_json"])["tentative"] is True
    assert db.one("SELECT status FROM jobs WHERE company='Full'")["status"] == "queued"


def test_thin_listing_needs_nine_and_valid_url(ctx):
    db = ctx.db
    db.upsert_job(listing("https://example.com/thin-nine", "", company="Nine"))
    db.upsert_job(listing("https://example.com/invalid", "", company="Invalid"))
    invalid = db.one("SELECT id FROM jobs WHERE company='Invalid'")["id"]
    db.set_job(invalid, url="nan", apply_url="nan")
    answer = {"fit": 9, "missing_skills": [], "seniority_flag": False,
              "requires_citizenship_or_clearance": False, "sponsorship_signal": "none", "location_us": True}
    filt.run(ctx, FakeLLM(lambda *_: answer), Profile(ctx))
    assert db.one("SELECT status FROM jobs WHERE company='Nine'")["status"] == "queued"
    assert db.one("SELECT status FROM jobs WHERE company='Invalid'")["status"] != "queued"


def test_daily_outage_skips_tailor_apply_and_alerts_once(ctx, monkeypatch):
    monkeypatch.setenv("JOBHUNT_HOME", str(ctx.root))
    monkeypatch.setattr(cli.discover, "run", lambda *_: {})
    monkeypatch.setattr(cli.filt, "run", lambda *_, **kwargs: {"quota_stop": False, "model_error": False,
                                                   "service_unavailable": True})
    monkeypatch.setattr(cli.tailor, "run", lambda *_: pytest.fail("tailor called"))
    monkeypatch.setattr(cli.apply_mod, "run", lambda *_: pytest.fail("apply called"))
    monkeypatch.setattr(cli.track, "gmail_fetch", lambda *_: [])
    monkeypatch.setattr(cli.track, "digest", lambda *_: "digest")
    sent = []
    monkeypatch.setattr(cli.Notifier, "send", lambda _self, _text, subject="": sent.append(subject))
    cli.main(["daily"])
    cli.main(["daily"])
    assert sent.count("jobhunt model outage") == 1


def test_interrupted_apply_needs_human_before_retry(ctx):
    confirm_profile(ctx)
    profile = Profile(ctx)
    profile.p["work_authorization"]["authorized_to_work_us_now"] = "Yes"
    profile.p["work_authorization"]["earliest_start_date"] = "Jan 2027"
    profile.p["preferences"]["salary_answer_text"] = "Open"
    profile.p["preferences"]["willing_to_relocate"] = "Yes"
    ctx.db.upsert_job(listing("https://example.com/apply", "Build Python services. " * 8))
    jid = ctx.db.one("SELECT id FROM jobs")["id"]
    (ctx.root / "config" / "profile.yaml").write_text(yaml.safe_dump(ctx.profile))
    ctx.db.kv_set("reviewed_profile_hash", profile.fingerprint())
    prepare_job(ctx, jid)
    class CrashingBackend:
        calls = 0
        def apply(self, *_):
            self.calls += 1
            raise SystemExit("process stopped")
    class Notifier:
        def __init__(self): self.sent = []
        def send(self, text, subject=""):
            self.sent.append(text)
    backend, notifier = CrashingBackend(), Notifier()
    with pytest.raises(SystemExit):
        apply_mod.run(ctx, profile, notifier, backend=backend)
    assert ctx.db.one("SELECT status FROM jobs")["status"] == "applying"
    assert ctx.db.one("SELECT status FROM applications")["status"] == "IN_PROGRESS"
    apply_mod.run(ctx, profile, notifier, backend=backend)
    assert backend.calls == 1
    req = ctx.db.one("SELECT * FROM human_requests")
    assert req["kind"] == "interrupted" and ctx.db.one("SELECT status FROM jobs")["status"] == "needs_human"
    assert hitl.resolve(ctx.db, "retry", req["id"], "Verified no employer submission")
    assert ctx.db.one("SELECT status FROM jobs")["status"] == "tailored"
