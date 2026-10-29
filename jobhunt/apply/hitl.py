"""Human requests are notified via ntfy and resolved with local CLI commands."""

from ..util import now_iso


def open_request(db, notifier, job, kind: str, text: str) -> int:
    cur = db.x("INSERT INTO human_requests(job_id,kind,question,created_at) VALUES(?,?,?,?)",
               (job["id"], kind, text, now_iso()))
    rid = cur.lastrowid
    db.set_job(job["id"], status="needs_human")
    hints = {"question": f"Run: jobhunt answer {rid} '<your answer>'",
             "attestation": f"Legal/EEO item. Run: jobhunt answer {rid} '<your answer>'",
             "captcha": f"CAPTCHA/verification. Apply manually, then run: jobhunt done {rid}",
             "login": f"Login/account wall. Apply manually, then run: jobhunt done {rid} (or jobhunt skip {rid})",
             "interrupted": f"Check for a submission. Run: jobhunt done {rid} / jobhunt retry {rid} / jobhunt skip {rid}"}
    notifier.send(f"[#{rid}] {job['company']} - {job['title']}\n{kind.upper()}: {text}\n{job['apply_url']}\n"
                  f"{hints.get(kind, f'Run: jobhunt done {rid} / jobhunt skip {rid}')}", subject="jobhunt: needs you")
    return rid


def resolve(db, cmd: str, rid: int, answer: str = "") -> bool:
    """Resolve one open request from the local CLI. Return False for invalid commands/IDs."""
    req = db.one("SELECT * FROM human_requests WHERE id=? AND status='open'", (rid,))
    if not req or cmd not in ("answer", "done", "skip", "retry"):
        return False
    if cmd == "retry" and req["kind"] != "interrupted":
        return False
    if cmd == "answer":
        if req["kind"] not in ("question", "attestation") or not answer.strip():
            return False
        answer = answer.strip()
        db.learn(req["question"].strip().lower(), answer)
        db.x("UPDATE human_requests SET answer=?,status='resolved',resolved_at=? WHERE id=?", (answer, now_iso(), rid))
        db.set_job(req["job_id"], status="tailored")
    else:
        db.x("UPDATE human_requests SET status='resolved',resolved_at=? WHERE id=?", (now_iso(), rid))
        if cmd == "retry":
            db.set_job(req["job_id"], status="tailored", notes="interrupted run checked; retry requested")
        elif cmd == "done":
            db.set_job(req["job_id"], status="applied", applied_at=now_iso(), notes="applied manually via HITL")
        else:
            db.set_job(req["job_id"], status="skipped", notes="skipped via HITL")
    return True
