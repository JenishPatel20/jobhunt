"""Recoverable application attempts with availability, document and submission gates."""
import hashlib
import json
from pathlib import Path

from .. import availability
from ..db import dumps
from ..util import now_iso, day_start_utc
from . import hitl
from .backends import make_backend, Result


def go_live_status(ctx) -> tuple[bool, str]:
    need = ctx.settings['apply']['dry_runs_required']
    done = int(ctx.db.kv_get('dry_runs_ok', '0'))
    if done < need:
        return False, f'{done}/{need} dry runs completed'
    if ctx.db.kv_get('live_enabled') != '1':
        return False, 'dry-run gate passed; run `jobhunt go-live` to enable reviewed submissions'
    return True, 'live enabled; workflow or per-application approval still required'


def recover(ctx, notifier):
    """Any unresolved claimed/ambiguous attempt is held, never automatically retried."""
    count = 0
    jobs = ctx.db.q("SELECT DISTINCT j.* FROM jobs j LEFT JOIN applications a ON a.job_id=j.id "
                    "WHERE j.status='applying' OR a.status IN ('IN_PROGRESS','SUBMISSION_UNKNOWN')")
    for job in jobs:
        if not ctx.db.one("SELECT id FROM human_requests WHERE job_id=? AND kind='interrupted' AND status='open'", (job['id'],)):
            hitl.open_request(ctx.db, notifier, job, 'interrupted',
                              'An application attempt may have submitted. Check the employer receipt or account before retrying.',
                              details={'resume_status': 'tailored'})
            count += 1
    return count


def documents_ready(ctx, job, profile):
    if not job.get('documents_valid') or job.get('document_profile_hash') != profile.fingerprint():
        return False
    for kind, path in [('resume', job.get('resume_path')), ('cover', job.get('cover_path'))]:
        if not path:
            if kind == 'cover' and not job.get('cover_required'):
                continue
            return False
        record = ctx.db.one('SELECT * FROM artifacts WHERE job_id=? AND kind=? AND path=? ORDER BY id DESC LIMIT 1', (job['id'], kind, path))
        if not record or not Path(path).is_file() or Path(path).suffix.lower() != '.pdf':
            return False
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != record['sha256'] or record['profile_hash'] != profile.fingerprint():
            return False
    return True


def run(ctx, profile, notifier, backend=None, force_dry: bool | None = None, limit: int | None = None, checker=None) -> dict:
    db, settings = ctx.db, ctx.settings['apply']
    stats = {'submitted': 0, 'dry_ok': 0, 'needs_human': recover(ctx, notifier), 'failed': 0, 'closed': 0, 'requeued': 0}
    live_ok, why = go_live_status(ctx)
    dry = not live_ok if force_dry is None else bool(force_dry)
    if not dry and not live_ok:
        raise SystemExit(f'Refusing live submit: {why}')
    backend = backend or make_backend(ctx.settings, ctx=ctx, profile=profile)
    cap = int(settings['max_applies_per_day'])
    done_today = db.one("SELECT COUNT(DISTINCT job_id) n FROM applications WHERE status IN ('SUBMITTED','RECONCILED_SUBMITTED') AND finished_at>=?", (day_start_utc(),))['n']
    for job in db.q("SELECT * FROM jobs WHERE status='tailored' ORDER BY fit_final DESC,boost DESC")[:limit if limit is not None else 10**6]:
        if not dry and done_today >= cap:
            break
        if job.get('duplicate_of'):
            hitl.open_request(db, notifier, job, 'duplicate',
                              'Possible duplicate of job ' + job['duplicate_of'] + '. Review both requisitions; resume keeps this one separate, skip excludes it.')
            stats['needs_human'] += 1
            continue
        required = profile.identity_missing()
        if required or not profile.reviewed():
            hitl.open_request(db, notifier, job, 'profile',
                              'Review the local candidate facts and required answers before browser preparation. Missing: ' + ', '.join(required),
                              details={'profile_hash': profile.fingerprint()})
            stats['needs_human'] += 1
            continue
        if not documents_ready(ctx, job, profile):
            db.set_job(job['id'], status='queued', documents_valid=0)
            stats['requeued'] += 1
            continue
        check = availability.check(ctx, job, 'before_application', checker)
        if check.state != 'open':
            if check.state == 'closed':
                stats['closed'] += 1
            elif db.one('SELECT status FROM jobs WHERE id=?', (job['id'],))['status'] == 'new':
                stats['requeued'] += 1
            else:
                hitl.open_request(db, notifier, job, 'availability', 'Employer posting could not be verified.', details={'evidence': check.evidence})
                stats['needs_human'] += 1
            continue
        provenance = {'profile_hash': profile.fingerprint(), 'answers': profile.answers_payload(job),
                      'artifacts': db.q('SELECT id,kind,path,sha256 FROM artifacts WHERE job_id=?', (job['id'],)),
                      'description_hash': job['description_hash']}
        workflow = 'greenhouse-classic-v1'
        attempt = db.claim(job['id'], 'dry' if dry else 'live', workflow, provenance)
        if attempt is None:
            continue
        job = {**job, '_attempt_id': attempt, '_submit_permitted': not dry, '_availability_checker': checker}
        try:
            result = backend.apply(job, profile.answers_payload(job), dry)
        except Exception as error:
            # Unknown backend timing is ambiguous; isolate this job and continue the queue.
            db.record(job['id'], 'backend_interrupted', {'attempt_id': attempt})
            result = Result('SUBMISSION_UNKNOWN', 'Backend interrupted: ' + type(error).__name__ + '; reconcile before retrying')
        if result.status == 'SUBMITTED' and (dry or not result.raw.get('confirmation')):
            result.status = 'SUBMISSION_UNKNOWN'
            result.detail = 'Backend reported submission without verifiable confirmation or during a dry run'
        if result.status == 'SUBMITTED':
            status = 'applied'
            stats['submitted'] += 1
            done_today += 1
        elif result.status == 'DRY_RUN_READY' and dry:
            status = 'dry_run_ok'
            stats['dry_ok'] += 1
        elif result.status in {'DOCUMENTS_REQUIRED', 'RECHECK_REQUIRED'}:
            status = db.one('SELECT status FROM jobs WHERE id=?', (job['id'],))['status']
            stats['requeued'] += 1
        elif result.status == 'NEEDS_HUMAN':
            status = 'needs_human'
            stats['needs_human'] += 1
        elif result.status == 'SUBMISSION_UNKNOWN':
            status = 'needs_human'
            stats['needs_human'] += 1
        else:
            result.status, status = 'FAILED', 'failed'
            stats['failed'] += 1
        at = now_iso()
        with db.conn:
            db.conn.execute('UPDATE applications SET status=?,finished_at=?,detail=?,evidence_json=? WHERE id=?',
                            (result.status, at, result.detail or result.text, dumps(result.raw), attempt))
            db.conn.execute('UPDATE jobs SET status=?,applied_at=coalesce(?,applied_at) WHERE id=?',
                            (status, at if status == 'applied' else None, job['id']))
            db.conn.execute('INSERT INTO journal(job_id,at,kind,detail_json) VALUES(?,?,?,?)',
                            (job['id'], at, 'attempt_finished', dumps({'attempt_id': attempt, 'status': result.status})))
            if result.status == 'DRY_RUN_READY' and dry:
                db.conn.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', ('dry_runs_ok', str(int(db.kv_get('dry_runs_ok', '0')) + 1)))
            confirmation = result.raw.get('confirmation', {})
            if result.status == 'SUBMITTED' and confirmation.get('individually_approved') and confirmation.get('shape'):
                db.conn.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', ('validated_form:' + workflow + ':' + confirmation['shape'], '1'))
        if result.status == 'NEEDS_HUMAN':
            details = {**result.raw.get('details', {}), 'profile_hash': profile.fingerprint()}
            hitl.open_request(db, notifier, job, result.kind or 'other', result.text, details=details)
        elif result.status == 'SUBMISSION_UNKNOWN':
            hitl.open_request(db, notifier, job, 'interrupted', result.detail)
        elif result.status == 'FAILED':
            hitl.open_request(db, notifier, job, 'issue', result.detail or 'Application preparation failed; review the local diagnostics before resuming.')
            stats['needs_human'] += 1
    return stats
