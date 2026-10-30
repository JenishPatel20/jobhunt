import argparse
import fcntl
import shutil
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

from . import apply as apply_mod
from . import discover, filter as filt, learn, tailor, track
from .config import Ctx, home
from .providers import make_provider
from .db import dumps, canonical_url
from .availability import identity_from_url
from .notify import Notifier
from .profile_store import Profile
from .util import today


def _alert_llm_outage(ctx, notifier):
    if ctx.db.kv_get("llm_outage_alert_day") != today():
        notifier.send("Model scoring is unavailable. Queue saved; no paid fallback. "
                      "Run `jobhunt llm-doctor` for details.", subject="jobhunt model outage")
        ctx.db.kv_set("llm_outage_alert_day", today())


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="jobhunt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("init", "preflight", "discover", "filter", "tailor", "status", "inbox", "track", "digest", "refresh",
              "learn", "daily", "go-live", "companies", "llm-doctor", "schedule-install",
              "schedule-status", "schedule-remove", "schedule-telegram-install", "schedule-telegram-status", "schedule-telegram-remove", 'profile-confirm', 'email-reviews', 'export', 'pause-live'):
        sub.add_parser(n)
    jobs = sub.add_parser("jobs", help="List saved jobs")
    jobs.add_argument("--limit", type=int, default=20, help="Number to show (default: 20)")
    jobs.add_argument("--all", action="store_true", help="Show every saved job")
    jobs.add_argument("--status", help="Show only jobs with this status, e.g. new or queued")
    answer = sub.add_parser("answer"); answer.add_argument("id", type=int); answer.add_argument("text", nargs="+")
    answer.add_argument('--scope', choices=['job', 'workflow'], default='job')
    for n in ('done', 'retry'):
        parser = sub.add_parser(n)
        parser.add_argument('id', type=int)
        parser.add_argument('text', nargs='+', help='Receipt or reconciliation evidence')
    for n in ('skip', 'resume', 'approve', 'takeover'):
        sub.add_parser(n).add_argument('id', type=int)
    for n in ('job', 'documents-retry', 'review-application'):
        sub.add_parser(n).add_argument('job_id')
    for n in ('workflow-enable', 'workflow-disable'):
        parser = sub.add_parser(n)
        parser.add_argument('workflow', choices=['greenhouse-classic-v1'])
        if n == 'workflow-enable':
            parser.add_argument('--evidence', required=True, help='Your review of trials and confirmation evidence')
    link = sub.add_parser('job-link')
    link.add_argument('job_id')
    link.add_argument('url')
    link.add_argument('--evidence', required=True)
    parser = sub.add_parser('email-link')
    parser.add_argument('event_id', type=int)
    parser.add_argument('job_id')
    parser = sub.add_parser('telegram-poll')
    parser.add_argument('--once', action='store_true')
    sp = sub.add_parser("sponsors-import"); sp.add_argument("file"); sp.add_argument("--label")
    pr = sub.add_parser("probe"); pr.add_argument("--top", type=int, default=200)
    ap_ = sub.add_parser("apply")
    g = ap_.add_mutually_exclusive_group(); g.add_argument("--dry-run", action="store_true"); g.add_argument("--live", action="store_true")
    ap_.add_argument("--limit", type=int)
    for n in ("filter", "tailor"):
        sub.choices[n].add_argument("--limit", type=int)
    a = ap.parse_args(argv)

    if a.cmd == 'init':
        root = home()
        source = Path(__file__).resolve().parents[1]
        (root / 'config').mkdir(parents=True, exist_ok=True)
        for file in (source / 'config' / 'examples').iterdir():
            destination = root / 'config' / file.name
            if file.is_file() and not destination.exists():
                shutil.copyfile(file, destination)
        for name in ('settings.yaml', 'titles.yaml', 'dealbreakers.yaml'):
            destination = root / 'config' / name
            if not destination.exists():
                shutil.copyfile(source / 'config' / name, destination)
        if not (root / '.env').exists():
            shutil.copyfile(source / '.env.example', root / '.env')
    ctx = Ctx()
    prof, notifier = Profile(ctx), Notifier(ctx.db)
    llm = make_provider(ctx.db, ctx.settings)

    if a.cmd == "init":
        print(f"Home: {home()}\nDB: {ctx.db.path}")
        a.cmd = "preflight"
    if a.cmd == "preflight":
        req, opt = prof.missing()
        print("REQUIRED before apply:" + ("".join(f"\n  - {k}" for k in req) or " none"))
        print("Optional TODOs:" + ("".join(f"\n  - {k}" for k in opt) or " none"))
        from .telegram import Telegram
        print('Reviewed profile:', prof.reviewed())
        print('Authorization answers currently confirmed:', prof.authorization_confirmed())
        print('Codex CLI:', 'available' if shutil.which('codex') else 'MISSING')
        print('Telegram:', 'configured' if Telegram(ctx.db).configured else 'not configured; local inbox retained')
    elif a.cmd == 'profile-confirm':
        if ctx.profile.get('example_profile', True) or prof.identity_missing():
            sys.exit('Replace fictional facts, set example_profile: false, and complete candidate identity before confirming.')
        try:
            prof.validate()
        except ValueError as error:
            sys.exit(str(error))
        ctx.db.kv_set('reviewed_profile_hash', prof.fingerprint())
        ctx.db.record(None, 'profile_reviewed', {'profile_hash': prof.fingerprint()})
        for job in ctx.db.q("SELECT * FROM jobs WHERE status IN ('needs_human','tailored','dry_run_ok','document_blocked')"):
            if ctx.db.one("SELECT id FROM applications WHERE job_id=? AND status IN ('IN_PROGRESS','SUBMISSION_UNKNOWN')", (job['id'],)):
                continue
            stale = [r for r in ctx.db.q("SELECT * FROM human_requests WHERE job_id=? AND status='open'", (job['id'],))
                     if json.loads(r['details_json']).get('profile_hash') not in (None, prof.fingerprint())]
            if stale or (job['document_profile_hash'] and job['document_profile_hash'] != prof.fingerprint()):
                for request in stale:
                    ctx.db.x("UPDATE human_requests SET status='stale' WHERE id=?", (request['id'],))
                ctx.db.set_job(job['id'], status='new', documents_valid=0)
        print('Current facts/answer bank marked reviewed. Any edit invalidates this confirmation. Unconfirmed authorization answers remain held.')
    elif a.cmd == 'pause-live':
        ctx.db.kv_set('live_enabled', '0')
        ctx.db.record(None, 'live_disabled', {})
        print('Live submissions paused.')
    elif a.cmd in {'workflow-enable', 'workflow-disable'}:
        from .workflows import enable, disable
        if a.cmd == 'workflow-enable':
            enable(ctx, a.workflow, a.evidence)
        else:
            disable(ctx, a.workflow)
    elif a.cmd == 'telegram-poll':
        import time
        import requests
        from .telegram import Telegram
        bot = Telegram(ctx.db, prof)
        while True:
            try:
                print('Telegram updates:', bot.poll(timeout=0 if a.once else 25))
            except (requests.RequestException, ValueError):
                print('Telegram unavailable; queue retained. Verify configuration/connectivity.')
                if a.once:
                    raise SystemExit(1)
                time.sleep(5)
            if a.once:
                break
    elif a.cmd == 'takeover':
        from .apply.browser import takeover
        takeover(ctx, a.id)
    elif a.cmd == 'job':
        print(dumps({name: ctx.db.q(f'SELECT * FROM {name} WHERE ' + ('id=?' if name == 'jobs' else 'job_id=?'), (a.job_id,))
                     for name in ('jobs', 'observations', 'snapshots', 'job_sources', 'journal', 'availability_checks', 'artifacts', 'applications', 'human_requests')}))
    elif a.cmd == 'job-link':
        url = canonical_url(a.url)
        identity = identity_from_url(url)
        job = ctx.db.one('SELECT * FROM jobs WHERE id=?', (a.job_id,))
        if not job or not identity or not a.evidence.strip():
            sys.exit('Provide an existing job, a supported employer posting URL, and your source evidence.')
        existing = ctx.db.one('SELECT job_id FROM job_sources WHERE url=?', (url,))
        identity_key = ':'.join(identity)
        identity_owner = ctx.db.one('SELECT id FROM jobs WHERE identity_key=? AND id<>?', (identity_key, a.job_id))
        if identity_owner:
            sys.exit('This requisition already belongs to job ' + identity_owner['id'] + '; review duplicates before linking.')
        if existing and existing['job_id'] != a.job_id:
            sys.exit('This URL already belongs to job ' + existing['job_id'] + '; review duplicates before linking.')
        if ctx.db.one("SELECT id FROM applications WHERE job_id=? AND status IN ('IN_PROGRESS','SUBMISSION_UNKNOWN','SUBMITTED','RECONCILED_SUBMITTED')", (a.job_id,)):
            sys.exit('Reconcile or retain the existing application before changing its employer link.')
        ctx.db.set_job(a.job_id, apply_url=url, documents_valid=0, status='new', score_key=None,
                       source=identity[0], board_slug=identity[1], source_job_id=identity[2], identity_key=identity_key)
        from .util import now_iso
        ctx.db.x('INSERT OR IGNORE INTO job_sources VALUES(?,?,?,?,?)', (url, a.job_id, identity[0], now_iso(), now_iso()))
        ctx.db.record(a.job_id, 'employer_link_reviewed', {'url': url, 'evidence': a.evidence})
        print('Employer link saved; availability and fit will be checked again.')
    elif a.cmd == 'documents-retry':
        row = ctx.db.one("SELECT id FROM jobs WHERE id=? AND status='document_blocked'", (a.job_id,))
        if not row:
            sys.exit('No document-blocked job with that ID')
        ctx.db.set_job(a.job_id, status='queued')
        print('Returned to document preparation.')
    elif a.cmd == 'review-application':
        from .apply import hitl
        job = ctx.db.one("SELECT * FROM jobs WHERE id=? AND status='dry_run_ok'", (a.job_id,))
        if not job:
            sys.exit('Review requires a successfully prepared dry run')
        try:
            checkpoint = json.loads((ctx.data_dir / 'browser' / job['id'] / 'checkpoint.json').read_text())
        except (OSError, ValueError):
            ctx.db.set_job(job['id'], status='queued', documents_valid=0)
            sys.exit('No current prepared-form checkpoint; returned the job to document preparation for a fresh dry run.')
        checkpoint['profile_hash'] = prof.fingerprint()
        print('Approval request:', hitl.open_request(ctx.db, notifier, job, 'approval', 'Review prepared form/documents before this application.', checkpoint))
    elif a.cmd == 'email-reviews':
        print(dumps(ctx.db.q("SELECT e.*,r.candidates_json FROM email_reviews r JOIN events e ON e.id=r.event_id WHERE r.status='open'")))
    elif a.cmd == 'email-link':
        if not track.link_event(ctx.db, a.event_id, a.job_id):
            sys.exit('No compatible open email review/job')
        print('Email event linked by your explicit review.')
    elif a.cmd == 'export':
        output = ctx.root / 'reports'
        output.mkdir(exist_ok=True)
        track.export_csv(ctx.db, output / 'jobs.csv')
        tables = ('jobs', 'observations', 'job_sources', 'snapshots', 'journal', 'availability_checks', 'assessments', 'artifacts', 'applications', 'human_requests', 'events', 'source_runs')
        (output / 'ledger.json').write_text(dumps({name: ctx.db.q(f'SELECT * FROM {name}') for name in tables}))
        print(f'Private local exports: {output / "jobs.csv"} and {output / "ledger.json"}')
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
        if a.cmd.endswith("install"):
            schedule.install(ctx, telegram="telegram" in a.cmd)
        elif a.cmd.endswith("remove"):
            schedule.remove(telegram="telegram" in a.cmd)
        else:
            schedule.status(telegram="telegram" in a.cmd)
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
        ctx.db.record(None, 'live_enabled', {'successful_dry_runs': done, 'required': need})
        ctx.db.x("UPDATE jobs SET status='tailored' WHERE status='dry_run_ok'")
        print("Live submissions enabled. Dry-run jobs re-queued.")
    elif a.cmd == "inbox":
        from .apply import hitl
        for r in ctx.db.q("SELECT h.id, h.kind, h.question, j.company, j.title FROM human_requests h "
                          "JOIN jobs j ON j.id=h.job_id WHERE h.status='open' ORDER BY h.id"):
            print(f"#{r['id']} {r['company']} - {r['title']} [{r['kind']}]\n  {r['question']}")
    elif a.cmd in ("answer", "done", "skip", "retry", 'resume', 'approve'):
        from .apply import hitl
        if not hitl.resolve(ctx.db, a.cmd, a.id, ' '.join(getattr(a, 'text', [])),
                            profile_hash=prof.fingerprint(), scope=getattr(a, 'scope', 'job')):
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
        apply_mod.recover(ctx, notifier)
        print("discover", discover.run(ctx))
        result = filt.run(ctx, llm, prof, limit=ctx.settings['filter'].get('daily_score_limit', 30))
        print("filter", result)
        until = llm.cooldown_until()
        outage = (result["quota_stop"] or result["model_error"] or result["service_unavailable"]
                  or (until is not None and until > datetime.now(timezone.utc)))
        if outage:
            print("Gemini scoring paused; tailoring and applying skipped for this run.")
            _alert_llm_outage(ctx, notifier)
        else:
            print("tailor", tailor.run(ctx, llm, prof, limit=ctx.settings['apply']['max_applies_per_day']))
            until = llm.cooldown_until()
            if until and until > datetime.now(timezone.utc):
                print("Gemini became unavailable during tailoring; applying skipped.")
                _alert_llm_outage(ctx, notifier)
            else:
                print("apply", apply_mod.run(ctx, prof, notifier, limit=ctx.settings['apply']['max_applies_per_day']))
        if ctx.settings.get('tracking', {}).get('gmail_enabled', False):
            try:
                print("track", track.ingest(ctx.db, track.gmail_fetch(ctx.root)))
            except Exception as e:
                print("track skipped:", type(e).__name__)
        from .telegram import Telegram
        Telegram(ctx.db, prof).flush()
        notifier.send(track.digest(ctx.db), subject="jobhunt daily digest")


def main(argv=None):
    """Prevent overlapping manual and scheduled pipeline runs."""
    args = list(sys.argv[1:] if argv is None else argv)
    read_only = {"init", "preflight", "status", "inbox", "jobs", "companies", "schedule-status", 'job', 'email-reviews',
                 'telegram-poll', 'answer', 'approve', 'resume', 'skip', 'done', 'retry', 'pause-live'}
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
