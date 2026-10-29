import argparse
import fcntl
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import apply as apply_mod
from . import discover, filter as filt, learn, tailor, track
from .config import Ctx, home
from .llm import Gemini
from .notify import Notifier
from .profile_store import Profile
from .util import today


def _alert_llm_outage(ctx, notifier):
    if ctx.db.kv_get("llm_outage_alert_day") != today():
        notifier.send("Gemini scoring is unavailable. Jobhunt saved the queue and will retry on the next run. "
                      "Run `jobhunt llm-doctor` for details.", subject="jobhunt Gemini outage")
        ctx.db.kv_set("llm_outage_alert_day", today())


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="jobhunt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("init", "preflight", "discover", "filter", "tailor", "status", "inbox", "track", "digest", "refresh",
              "learn", "daily", "go-live", "companies", "llm-doctor", "schedule-install",
              "schedule-status", "schedule-remove"):
        sub.add_parser(n)
    jobs = sub.add_parser("jobs", help="List saved jobs")
    jobs.add_argument("--limit", type=int, default=20, help="Number to show (default: 20)")
    jobs.add_argument("--all", action="store_true", help="Show every saved job")
    jobs.add_argument("--status", help="Show only jobs with this status, e.g. new or queued")
    answer = sub.add_parser("answer"); answer.add_argument("id", type=int); answer.add_argument("text", nargs="+")
    for n in ("done", "skip", "retry"):
        sub.add_parser(n).add_argument("id", type=int)
    sp = sub.add_parser("sponsors-import"); sp.add_argument("file"); sp.add_argument("--label")
    pr = sub.add_parser("probe"); pr.add_argument("--top", type=int, default=200)
    ap_ = sub.add_parser("apply")
    g = ap_.add_mutually_exclusive_group(); g.add_argument("--dry-run", action="store_true"); g.add_argument("--live", action="store_true")
    ap_.add_argument("--limit", type=int)
    for n in ("filter", "tailor"):
        sub.choices[n].add_argument("--limit", type=int)
    a = ap.parse_args(argv)

    ctx = Ctx()
    prof, notifier = Profile(ctx), Notifier(ctx.db)
    llm = Gemini(ctx.db, ctx.settings)

    if a.cmd == "init":
        print(f"Home: {home()}\nDB: {ctx.db.path}")
        a.cmd = "preflight"
    if a.cmd == "preflight":
        req, opt = prof.missing()
        print("REQUIRED before apply:" + ("".join(f"\n  - {k}" for k in req) or " none"))
        print("Optional TODOs:" + ("".join(f"\n  - {k}" for k in opt) or " none"))
        import os
        print("GEMINI_API_KEY:", "set" if os.environ.get("GEMINI_API_KEY") else "MISSING")
        print("ntfy:", "set" if os.environ.get("NTFY_TOPIC") else "not set (console fallback)")
        print("Gemini CLI:", "available" if shutil.which("gemini") else "MISSING")
    elif a.cmd == "sponsors-import":
        print(discover.sponsors.import_file(ctx.db, a.file, a.label))
    elif a.cmd == "probe":
        print("boards found:", discover.probe_companies(ctx, a.top))
    elif a.cmd == "companies":
        for c in ctx.db.q("SELECT ats, slug, display_name, verified FROM companies ORDER BY verified DESC, display_name"):
            print(f"{'OK ' if c['verified'] else '?? '} {c['ats']:<10} {c['slug']:<30} {c['display_name']}")
    elif a.cmd == "jobs":
        if a.limit < 1:
            ap.error("jobs --limit must be at least 1")
        where = "WHERE status=?" if a.status else ""
        args = (a.status,) if a.status else ()
        count = ctx.db.one(f"SELECT COUNT(*) n FROM jobs {where}", args)["n"]
        query = (f"SELECT company, title, location, source, status, url FROM jobs {where} "
                 "ORDER BY discovered_at DESC, rowid DESC")
        if not a.all:
            query += " LIMIT ?"
            args += (a.limit,)
        rows = ctx.db.q(query, args)
        print(f"Showing {len(rows)} of {count} saved jobs")
        for i, job in enumerate(rows, 1):
            print(f"{i}. {job['title']} — {job['company']} ({job['location'] or 'location unknown'}) "
                  f"[{job['status']}; {job['source']}]\n   {job['url']}")
    elif a.cmd == "discover":
        print(discover.run(ctx))
    elif a.cmd == "llm-doctor":
        from .doctor import run
        if not run(ctx):
            raise SystemExit(1)
    elif a.cmd.startswith("schedule-"):
        from . import schedule
        if a.cmd == "schedule-install":
            schedule.install(ctx)
        elif a.cmd == "schedule-remove":
            schedule.remove()
        else:
            schedule.status()
    elif a.cmd == "filter":
        print(filt.run(ctx, llm, prof, a.limit))
    elif a.cmd == "tailor":
        print(tailor.run(ctx, llm, prof, a.limit))
    elif a.cmd == "apply":
        print(apply_mod.run(ctx, prof, notifier, force_dry=True if a.dry_run else (False if a.live else None), limit=a.limit))
    elif a.cmd == "go-live":
        ok, why = apply_mod.go_live_status(ctx)
        done = int(ctx.db.kv_get("dry_runs_ok", "0")); need = ctx.settings["apply"]["dry_runs_required"]
        if done < need:
            sys.exit(f"Not yet: {why}. Review the dry-run results first.")
        ctx.db.kv_set("live_enabled", "1")
        ctx.db.x("UPDATE jobs SET status='tailored' WHERE status='dry_run_ok'")
        print("Live submissions enabled. Dry-run jobs re-queued.")
    elif a.cmd == "inbox":
        from .apply import hitl
        for r in ctx.db.q("SELECT h.id, h.kind, h.question, j.company, j.title FROM human_requests h "
                          "JOIN jobs j ON j.id=h.job_id WHERE h.status='open' ORDER BY h.id"):
            print(f"#{r['id']} {r['company']} - {r['title']} [{r['kind']}]\n  {r['question']}")
    elif a.cmd in ("answer", "done", "skip", "retry"):
        from .apply import hitl
        if not hitl.resolve(ctx.db, a.cmd, a.id, " ".join(a.text) if a.cmd == "answer" else ""):
            sys.exit(f"No compatible open request #{a.id}")
        print(f"Request #{a.id} resolved ({a.cmd}).")
    elif a.cmd == "track":
        msgs = track.gmail_fetch(ctx.root)
        print("events:", track.ingest(ctx.db, msgs), "| csv rows:", track.export_csv(ctx.db, ctx.root / "tracker.csv"))
    elif a.cmd == "digest":
        notifier.send(track.digest(ctx.db), subject="jobhunt daily digest")
    elif a.cmd == "refresh":
        print(learn.refresh(ctx, llm, prof, notifier))
    elif a.cmd == "learn":
        print(learn.learning_plan(ctx, llm, ctx.db))
    elif a.cmd == "status":
        for r in ctx.db.q("SELECT status, COUNT(*) n FROM jobs GROUP BY status ORDER BY n DESC"):
            print(f"{r['status']:<16}{r['n']}")
        print("LLM calls today:", ctx.db.llm_calls_today(), "| dry runs ok:", ctx.db.kv_get("dry_runs_ok", "0"))
        print("Apply gate:", apply_mod.go_live_status(ctx)[1])
        until = llm.cooldown_until()
        if until:
            if until > datetime.now(timezone.utc):
                print("Gemini retry after:", until.astimezone().strftime("%Y-%m-%d %I:%M %p %Z"))
    elif a.cmd == "daily":
        print("discover", discover.run(ctx))
        result = filt.run(ctx, llm, prof)
        print("filter", result)
        until = llm.cooldown_until()
        outage = (result["quota_stop"] or result["model_error"] or result["service_unavailable"]
                  or (until is not None and until > datetime.now(timezone.utc)))
        if outage:
            print("Gemini scoring paused; tailoring and applying skipped for this run.")
            _alert_llm_outage(ctx, notifier)
        else:
            print("tailor", tailor.run(ctx, llm, prof))
            until = llm.cooldown_until()
            if until and until > datetime.now(timezone.utc):
                print("Gemini became unavailable during tailoring; applying skipped.")
                _alert_llm_outage(ctx, notifier)
            else:
                print("apply", apply_mod.run(ctx, prof, notifier))
        try:
            print("track", track.ingest(ctx.db, track.gmail_fetch(ctx.root)))
        except Exception as e:
            print("track skipped:", e)
        notifier.send(track.digest(ctx.db), subject="jobhunt daily digest")


def main(argv=None):
    """Prevent overlapping manual and scheduled pipeline runs."""
    args = list(sys.argv[1:] if argv is None else argv)
    read_only = {"init", "preflight", "status", "inbox", "jobs", "companies", "schedule-status"}
    if not args or args[0] in read_only:
        return _main(args)
    lock_path = Path(home()) / "data" / "jobhunt.lock"
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another jobhunt run is active; try again after it finishes.")
        return _main(args)


if __name__ == "__main__":
    main()
