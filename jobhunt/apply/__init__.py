"""MODULE 5 - Apply. Fully automatic within guardrails; pauses (non-blocking) for the human."""
from ..util import now_iso, today
from . import hitl
from .backends import make_backend


def go_live_status(ctx) -> tuple[bool, str]:
    need = ctx.settings["apply"]["dry_runs_required"]
    done = int(ctx.db.kv_get("dry_runs_ok", "0"))
    if done < need:
        return False, f"{done}/{need} dry runs completed"
    if ctx.db.kv_get("live_enabled") != "1":
        return False, "dry-run gate passed; run `jobhunt go-live` to enable submissions"
    return True, "live enabled"


def run(ctx, profile, notifier, backend=None, force_dry: bool | None = None, limit: int | None = None) -> dict:
    db, a = ctx.db, ctx.settings["apply"]
    stats = {"submitted": 0, "dry_ok": 0, "needs_human": 0, "failed": 0}
    # A previous process may have stopped after a website submit. Never retry it blindly.
    for interrupted in db.q("SELECT * FROM jobs WHERE status='applying'"):
        if not db.one("SELECT id FROM human_requests WHERE job_id=? AND status='open'", (interrupted["id"],)):
            hitl.open_request(db, notifier, interrupted, "interrupted",
                              "The application run stopped before its result was saved. Check the website or email "
                              "for a submission, then mark done, retry, or skip.")
            stats["needs_human"] += 1
    req, _ = profile.missing()
    if req:
        raise SystemExit("Preflight failed - fill these in config/profile.yaml first:\n  " + "\n  ".join(req))
    live_ok, why = go_live_status(ctx)
    dry = (not live_ok) if force_dry is None else bool(force_dry)
    if not dry and not live_ok:
        raise SystemExit(f"Refusing live submit: {why}")
    backend = backend or make_backend(ctx.settings)
    cap = a["max_applies_per_day"]
    done_today = db.one("SELECT COUNT(*) n FROM applications WHERE mode='live' AND status='SUBMITTED' AND started_at>=?", (today(),))["n"]
    for job in db.q("SELECT * FROM jobs WHERE status='tailored' ORDER BY fit_final DESC")[: limit or 10**6]:
        if not dry and done_today >= cap:
            break
        started = now_iso()
        claim = db.x("INSERT INTO applications(job_id,mode,status,started_at) VALUES(?,?,?,?)",
                     (job["id"], "dry" if dry else "live", "IN_PROGRESS", started))
        db.set_job(job["id"], status="applying")
        res = backend.apply(job, profile.answers_payload(), dry)
        db.x("UPDATE applications SET status=?,finished_at=?,detail=? WHERE id=?",
             (res.status, now_iso(), res.detail or res.text, claim.lastrowid))
        if res.status == "SUBMITTED" and not dry:
            db.set_job(job["id"], status="applied", applied_at=now_iso())
            stats["submitted"] += 1
            done_today += 1
        elif res.status == "DRY_RUN_READY" and dry:
            db.set_job(job["id"], status="dry_run_ok")
            db.kv_set("dry_runs_ok", int(db.kv_get("dry_runs_ok", "0")) + 1)
            stats["dry_ok"] += 1
        elif res.status == "NEEDS_HUMAN":
            # answer bank may already know it (agent is told not to guess, but we double-check here)
            if res.kind == "question":
                ans, _ = profile.answer(res.text)
                if ans:
                    db.learn(res.text.strip().lower(), ans)
            hitl.open_request(db, notifier, job, res.kind or "other", res.text)
            stats["needs_human"] += 1
        else:
            db.set_job(job["id"], status="failed", notes=res.detail[:500])
            stats["failed"] += 1
    return stats
