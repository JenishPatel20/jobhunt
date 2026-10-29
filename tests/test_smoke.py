import json
from pathlib import Path

import pytest
import yaml

from jobhunt import apply as apply_mod, filter as filt, learn, tailor, track
from jobhunt.apply import hitl
from jobhunt.apply.backends import FakeBackend, GeminiCLIBackend, Result, parse_final
from jobhunt.config import Ctx
from jobhunt.discover import sponsors
from jobhunt.llm import FakeLLM, Gemini, ModelUnavailable, QuotaExceeded, TemporaryUnavailable
from jobhunt.profile_store import Profile
from jobhunt.notify import Notifier

class Notif:
    def __init__(self): self.sent = []
    def send(self, t, subject=""): self.sent.append(t); return "fake"


def job(**kw):
    base = dict(source="t", company="Acme Inc", title="Software Engineer I", location="New York, NY",
                url="https://x/1", apply_url="https://x/1", description="Build services. Python. 0-2 years experience.")
    base.update(kw)
    if "url" in kw and "apply_url" not in kw:
        base["apply_url"] = kw["url"]
    return base


def test_location_and_years():
    assert filt.classify_location("San Francisco, CA") == "us"
    assert filt.classify_location("Toronto, ON, Canada") == "non_us"
    assert filt.classify_location("London, UK; New York, NY") == "us"
    assert filt.classify_location("Remote") == "unknown"
    assert filt.required_years("5+ years of professional experience required") == 5
    assert filt.required_years("Bonus: 5+ years experience preferred. 2 years experience.") == 2


@pytest.mark.parametrize("kw,ok", [
    ({}, True),
    ({"title": "Senior Software Engineer"}, False),
    ({"title": "Software Engineer II"}, False),
    ({"title": "Sales Engineer"}, False),
    ({"title": "Account Executive"}, False),
    ({"title": "Software Engineer Intern"}, False),
    ({"title": "Associate Attorney"}, False),
    ({"title": "Associate Software Engineer"}, True),
    ({"title": "New Grad - Member of Technical Staff"}, True),
    ({"title": "Data Analyst", "description": "Use Python, SQL and pandas for dashboards. 1 year experience."}, True),
    ({"title": "Data Analyst", "description": "Excel reporting and sales support."}, False),
    ({"title": "Senior Data Analyst", "description": "Python SQL pandas"}, False),
    ({"location": "Bangalore, India"}, False),
    ({"description": "We are unable to sponsor work visas."}, False),
    ({"description": "Candidates must be authorized to work in the US without sponsorship."}, False),
    ({"description": "Must be authorized to work in the United States."}, True),
    ({"description": "Active TS/SCI clearance required."}, False),
    ({"description": "Must be a U.S. citizen."}, False),
    ({"description": "5+ years of experience required."}, False),
])
def test_hard_rules(ctx, kw, ok):
    assert filt.Rules(ctx).check(job(**kw))[0] is ok


def test_sponsor_import_and_tier(ctx, tmp_path):
    f = tmp_path / "lca.csv"
    f.write_text("EMPLOYER_NAME,CASE_STATUS,VISA_CLASS,JOB_TITLE\n"
                 "ACME INC.,Certified,H-1B,Software Engineer\nACME INC.,Certified,H-1B,Data Scientist\n"
                 "ACME INC.,Denied,H-1B,Software Engineer\nOther LLC,Certified,H-1B,Accountant\n")
    sponsors.import_file(ctx.db, str(f))
    assert sponsors.tier_for(ctx.db, "acme", {"strong": 50, "some": 2, "weak": 1}) == "some"
    assert sponsors.tier_for(ctx.db, "other", {"strong": 50, "some": 2, "weak": 1}) == "weak"
    assert sponsors.tier_for(ctx.db, "nobody", {"strong": 50, "some": 2, "weak": 1}) is None


def fit_llm(fit, **extra):
    return FakeLLM(lambda s, p, j: {"fit": fit, "reasons": [], "missing_skills": ["kubernetes"], "seniority_flag": False,
                                     "requires_citizenship_or_clearance": False, "sponsorship_signal": "none",
                                     "location_us": True, **extra})


def test_filter_pipeline_bands_and_boost(ctx):
    db = ctx.db
    complete = "Build Python services and APIs with a collaborative team. Learn system design and testing. " * 2
    db.upsert_job(job(url="https://x/a", title="Software Engineer I", company="Acme Inc", description=complete))
    db.upsert_job(job(url="https://x/b", title="Backend Engineer", company="Beta Co", description=complete))
    db.upsert_job(job(url="https://x/c", title="Senior Backend Engineer", company="Gamma"))
    db.x("INSERT INTO sponsors VALUES('acme','Acme',100,80,'2025','t')")
    prof = Profile(ctx)
    st = filt.run(ctx, fit_llm(6), prof)
    assert st["rejected"] == 1
    acme = db.one("SELECT * FROM jobs WHERE company_norm='acme'")
    beta = db.one("SELECT * FROM jobs WHERE company_norm='beta'")
    assert acme["status"] == "queued" and acme["fit_final"] == 7.0       # 6 + strong boost 1.0
    assert beta["status"] == "gap"                                        # 6, no boost
    assert json.loads(beta["missing_skills"]) == ["kubernetes"]


def test_filter_llm_rejection_and_quota(ctx):
    ctx.db.upsert_job(job())
    assert filt.run(ctx, fit_llm(9, sponsorship_signal="negative"), Profile(ctx))["rejected"] == 1

    def boom(*a): raise QuotaExceeded("cap")
    ctx.db.upsert_job(job(url="https://x/2", title="ML Engineer", company="Zed"))
    st = filt.run(ctx, FakeLLM(boom), Profile(ctx))
    assert st["quota_stop"] and ctx.db.one("SELECT status FROM jobs WHERE company_norm='zed'")["status"] == "passed_rules"


def test_filter_stops_after_unavailable_model(ctx):
    ctx.db.upsert_job(job(url="https://x/model-1", company="First"))
    ctx.db.upsert_job(job(url="https://x/model-2", company="Second"))
    def unavailable(*_):
        raise ModelUnavailable("model unavailable")
    llm = FakeLLM(unavailable)
    stats = filt.run(ctx, llm, Profile(ctx))
    assert stats["model_error"] is True
    assert llm.calls == 1
    assert ctx.db.one("SELECT COUNT(*) n FROM jobs WHERE status='passed_rules'")["n"] == 2


def test_filter_stops_after_temporary_service_failure(ctx):
    ctx.db.upsert_job(job())
    def unavailable(*_):
        raise TemporaryUnavailable("service overloaded")
    stats = filt.run(ctx, FakeLLM(unavailable), Profile(ctx))
    assert stats["service_unavailable"] is True
    assert ctx.db.one("SELECT status FROM jobs")["status"] == "passed_rules"


def test_gemini_retries_503_then_uses_fallback(ctx, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    llm = Gemini(ctx.db, ctx.settings)
    monkeypatch.setattr(llm, "_gate", lambda: None)
    monkeypatch.setattr("jobhunt.llm.time.sleep", lambda _: None)
    calls = []
    class Response:
        text = ""
        def __init__(self, code): self.status_code = code
        def json(self): return {"candidates": [{"content": {"parts": [{"text": "ready"}]}}]}
    def fake_post(url, **_):
        calls.append(url)
        return Response(503 if len(calls) <= 3 else 200)
    monkeypatch.setattr("jobhunt.llm.requests.post", fake_post)
    assert llm.text("system", "prompt") == "ready"
    assert all("gemini-3.8-flash" in url for url in calls[:2])
    assert all("gemini-3.6-flash" in url for url in calls[2:4])
    assert llm.text("system", "again") == "ready"
    assert len(calls) == 5 and "gemini-3.6-flash" in calls[4]


def test_tailor_blocks_fabrication(ctx):
    prof = Profile(ctx)
    j = job(company="Acme", description="Kubernetes AWS Python")

    def handler(s, p, as_json):
        return {"experience": [{"id": "example-api", "bullets": [
                    {"src": "api-1", "text": "Built a FastAPI/PostgreSQL service with 50+ REST endpoints on AWS."},   # bad number + AWS
                    {"src": "api-2", "text": "Built a Python scraping and enrichment pipeline using BeautifulSoup and S3."}]}],
                "projects": [{"id": "example-ml", "bullets": [{"src": "ml-1", "text": "Trained 9 classifiers with scikit-learn and XGBoost."}]}],
                "skills": {"Languages": ["SQL", "Python", "Haskell"]}}
    plan, notes = tailor.build_plan(ctx, FakeLLM(handler), prof, j)
    g = next(e for e in plan["experience"] if e["id"] == "example-api")
    assert g["bullets"][0]["text"] == prof.bullets()["api-1"]              # reverted to locked text
    assert "pipeline using BeautifulSoup" in g["bullets"][1]["text"]      # clean rewrite accepted
    assert {e["id"] for e in plan["experience"]} == {"example-api", "example-ops", "example-web"}
    assert plan["skills"]["Languages"][:2] == ["SQL", "Python"] and "Haskell" not in plan["skills"]["Languages"]
    assert any("reverted" in n for n in notes)


def test_tailor_run_and_cover_letter_guard(ctx, monkeypatch):
    monkeypatch.setattr(tailor, "html_to_pdf", lambda *_: False)
    ctx.db.upsert_job(job(company="Acme"))
    jid = ctx.db.one("SELECT id FROM jobs")["id"]
    ctx.db.set_job(jid, status="queued", fit_final=8)
    prof = Profile(ctx)

    def handler(s, p, as_json):
        if as_json:
            raise_ = {"experience": [], "projects": [], "skills": {}}
            return raise_
        return "Hello,\nI built REST services in Python with FastAPI and PostgreSQL. I also ran Kubernetes clusters.\nAlex Example"
    st = tailor.run(ctx, FakeLLM(handler), prof)
    row = ctx.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    assert row["status"] == "tailored" and row["resume_path"].endswith((".pdf", ".html"))
    assert row["cover_path"] is None                                      # Kubernetes claim rejected
    assert "Alex Example" in Path(ctx.out_dir / jid / "resume.html").read_text()


def tailored_job(ctx, url="https://x/9", company="Acme"):
    ctx.db.upsert_job(job(url=url, company=company))
    jid = ctx.db.job_id(url)
    ctx.db.set_job(jid, status="tailored", fit_final=8, resume_path="/tmp/r.pdf")
    return jid


def fill_profile(ctx):
    w, pf = ctx.profile["work_authorization"], ctx.profile["preferences"]
    w["authorized_to_work_us_now"], w["earliest_start_date"] = "Yes", "Jan 2027"
    pf["salary_answer_text"], pf["willing_to_relocate"] = "Open", "Yes"


def test_apply_preflight_blocks_without_answers(ctx):
    tailored_job(ctx)
    ctx.profile["preferences"]["salary_answer_text"] = None
    with pytest.raises(SystemExit):
        apply_mod.run(ctx, Profile(ctx), Notif(), backend=FakeBackend())


def test_apply_dry_gate_and_live_refusal(ctx):
    fill_profile(ctx)
    jid = tailored_job(ctx)
    fb, n = FakeBackend(), Notif()
    st = apply_mod.run(ctx, Profile(ctx), n, backend=fb)                  # default => dry (gate not passed)
    assert fb.calls == [(jid, True)] and st["dry_ok"] == 1
    with pytest.raises(SystemExit):
        apply_mod.run(ctx, Profile(ctx), n, backend=fb, force_dry=False)  # live refused
    ctx.db.kv_set("dry_runs_ok", 20)
    assert apply_mod.go_live_status(ctx)[0] is False                      # still needs explicit go-live
    ctx.db.kv_set("live_enabled", "1")
    ctx.db.set_job(jid, status="tailored")
    st = apply_mod.run(ctx, Profile(ctx), n, backend=fb)
    assert st["submitted"] == 1 and ctx.db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "applied"


def test_hitl_flow_answer_and_done(ctx):
    fill_profile(ctx)
    jid = tailored_job(ctx)
    n = Notif()
    fb = FakeBackend([Result("NEEDS_HUMAN", kind="question", text="Why do you want to work here?")])
    st = apply_mod.run(ctx, Profile(ctx), n, backend=fb)
    assert st["needs_human"] == 1 and "jobhunt answer 1" in n.sent[-1]
    assert ctx.db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "needs_human"
    assert hitl.resolve(ctx.db, "answer", 1, "Because I like the product.")
    assert not hitl.resolve(ctx.db, "answer", 999, "nope")
    assert ctx.db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "tailored"
    assert "learned_answers_by_question" in Profile(ctx).answers_payload()
    # captcha -> done
    fb = FakeBackend([Result("NEEDS_HUMAN", kind="captcha", text="reCAPTCHA")])
    apply_mod.run(ctx, Profile(ctx), n, backend=fb)
    assert not hitl.resolve(ctx.db, "answer", 2, "anything")
    assert hitl.resolve(ctx.db, "done", 2)
    assert ctx.db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "applied"


def test_ntfy_notification(ctx, monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "private-example-topic")
    monkeypatch.setenv("NTFY_TOKEN", "test-token")
    monkeypatch.setenv("NTFY_BASE_URL", "https://ntfy.sh")
    calls = []
    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return type("Response", (), {"ok": True})()
    monkeypatch.setattr("jobhunt.notify.requests.post", fake_post)
    assert Notifier(ctx.db).send("Please help", subject="jobhunt: needs you") == "ntfy"
    assert calls[0][0] == "https://ntfy.sh/private-example-topic"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-token"
    assert calls[0][1]["data"] == b"Please help"


def test_gemini_backend_uses_isolated_browser_tools(ctx, monkeypatch):
    calls = []
    def fake_run(cmd, **kwargs):
        cfg = json.loads((Path(kwargs["cwd"]) / ".gemini" / "settings.json").read_text())
        calls.append((cmd, cfg))
        return type("Process", (), {"stdout": json.dumps({"response": '{"status":"DRY_RUN_READY","kind":"","text":"","detail":"ready"}'}),
                                    "stderr": "", "returncode": 0})()
    monkeypatch.setattr("jobhunt.apply.backends.subprocess.run", fake_run)
    backend = GeminiCLIBackend(ctx.settings)
    result = backend.apply({"apply_url": "https://example.com", "company": "Example", "title": "Engineer",
                            "resume_path": "/tmp/resume.pdf", "cover_path": None}, {}, True)
    assert result.status == "DRY_RUN_READY"
    assert calls[0][0][0] == "gemini"
    assert calls[0][1]["tools"]["core"] == ["playwright__*", "mcp_playwright_*"]
    assert calls[0][1]["mcp"]["allowed"] == ["playwright"]


def test_answer_bank(ctx):
    fill_profile(ctx)
    p = Profile(ctx)
    assert p.answer("Will you now or in the future require sponsorship for employment visa status?")[0] == "Yes"
    assert p.answer("Are you legally authorized to work in the United States?")[0] == "Yes"
    assert p.answer("I certify the above is true")[0] is None             # attestation always escalates
    assert p.answer("What is your favorite color?")[0] is None


def test_salary_answers_for_text_and_numeric_fields(ctx):
    p = Profile(ctx)
    answers = p.answers_payload()
    assert answers["salary"].startswith("My annual base salary expectation")
    assert answers["salary_min_usd"] == 90000
    assert answers["salary_max_usd"] == 100000
    assert answers["relocation"] == "Yes"


def test_parse_final():
    r = parse_final('blah blah\n{"status":"NEEDS_HUMAN","kind":"captcha","text":"cf","detail":"x"}')
    assert (r.status, r.kind) == ("NEEDS_HUMAN", "captcha")
    assert parse_final("no json").status == "FAILED"


def test_track_classify_and_ingest(ctx):
    assert track.classify_email("Thank you for applying to Acme") == "received"
    assert track.classify_email("Update on your application", "Unfortunately we will not be moving forward") == "rejection"
    assert track.classify_email("Next steps", "please schedule a call") == "interview"
    assert track.classify_email("Your CodeSignal assessment") == "assessment"
    assert track.classify_email("Weekly newsletter") is None
    jid = tailored_job(ctx, company="Acme Inc")
    ctx.db.set_job(jid, status="applied")
    n = track.ingest(ctx.db, [{"id": "m1", "from": "Acme Recruiting <jobs@acme.com>", "subject": "Interview with Acme",
                               "snippet": "", "date": "x"}])
    assert n == 1 and ctx.db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "interviewing"
    assert track.ingest(ctx.db, [{"id": "m1", "from": "a", "subject": "Interview", "date": "x"}]) == 0   # idempotent
    assert "jobhunt digest" in track.digest(ctx.db)


def test_skill_gaps(ctx):
    for i, (st, ms) in enumerate([("gap", ["Kubernetes", "AWS"]), ("queued", ["AWS"]), ("rejected", None)]):
        ctx.db.upsert_job(job(url=f"https://x/{i}", title=f"Software Engineer {i}", company=f"C{i}"))
        ctx.db.set_job(ctx.db.job_id(f"https://x/{i}"), status=st, missing_skills=json.dumps(ms) if ms else None)
    assert learn.skill_gaps(ctx.db)[0] == ("aws", 3)
