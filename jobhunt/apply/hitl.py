"""Durable human requests, exact scoped answers, and explicit reconciliation."""
import json
import re
from datetime import date

from ..db import dumps
from ..util import now_iso


def open_request(db, notifier, job, kind: str, text: str, details=None) -> int:
    existing = db.one("SELECT id FROM human_requests WHERE job_id=? AND kind=? AND question=? AND status='open'",
                      (job['id'], kind, text))
    if existing:
        return existing['id']
    details = dict(details or {})
    details.setdefault('resume_status', 'tailored')
    at = now_iso()
    with db.conn:
        cur = db.conn.execute('INSERT INTO human_requests(job_id,kind,question,created_at,details_json) VALUES(?,?,?,?,?)',
                              (job['id'], kind, text, at, dumps(details)))
        rid = cur.lastrowid
        db.conn.execute("UPDATE jobs SET status='needs_human' WHERE id=?", (job['id'],))
        db.conn.execute('INSERT INTO journal(job_id,at,kind,detail_json) VALUES(?,?,?,?)',
                        (job['id'], at, 'human_requested', dumps({'request_id': rid, 'kind': kind})))
    hints = {'question': f'/answer {rid} <exact answer>', 'attestation': f'/answer {rid} <approved answer>',
             'approval': f'/approve {rid}', 'interrupted': f'/done {rid} <receipt evidence> OR /retry {rid} <reconciliation evidence>',
             'login': f'On your Mac: jobhunt takeover {rid}; then /resume {rid}',
             'captcha': f'On your Mac: jobhunt takeover {rid}; then /resume {rid}',
             'profile': f'Confirm local profile/answers, run jobhunt profile-confirm, then /resume {rid}'}
    message = (f'[#{rid}] {job.get("company", "")} — {job.get("title", "")}\n{kind.upper()}: {text}\n'
               + ('Choices: ' + ', '.join(str(c.get('text', c)) if isinstance(c, dict) else str(c) for c in details['choices']) + '\n' if details.get('choices') else '') +
               ('Required format: ' + details['format'] + '\n' if details.get('format') else '') +
               f'{job.get("apply_url", "")}\n{hints.get(kind, f"Review on your Mac; /resume {rid} or /skip {rid}")}\n'
               'Only this job is paused. Answers resume preparation; they do not independently authorize submission.')
    if hasattr(notifier, 'enqueue'):
        notifier.enqueue(message, rid)
    else:
        notifier.send(message, subject='jobhunt: needs you')
    return rid


def resolve(db, cmd: str, rid: int, answer: str = '', profile_hash=None, scope='job') -> bool:
    """Atomically consume one request. No automatic retries of ambiguous submissions."""
    req = db.one("SELECT * FROM human_requests WHERE id=? AND status='open'", (rid,))
    if not req or cmd not in {'answer', 'done', 'skip', 'retry', 'resume', 'approve'}:
        return False
    details = json.loads(req['details_json'] or '{}')
    job = db.one('SELECT * FROM jobs WHERE id=?', (req['job_id'],))
    answer = answer.strip()
    if cmd == 'answer' and (req['kind'] not in {'question', 'attestation'} or not answer):
        return False
    if cmd == 'retry' and (req['kind'] != 'interrupted' or not answer):
        return False
    if cmd == 'done' and not answer:
        return False
    if cmd == 'approve' and (req['kind'] != 'approval' or not details.get('fingerprint')):
        return False
    if cmd == 'resume' and req['kind'] in {'interrupted', 'approval', 'question', 'attestation'}:
        return False
    if cmd in {'answer', 'approve'} and details.get('profile_hash') and profile_hash != details['profile_hash']:
        return False
    at = now_iso()
    state = details.get('resume_status', 'tailored')
    with db.conn:
        claimed = db.conn.execute("UPDATE human_requests SET status='resolved',answer=?,resolved_at=? WHERE id=? AND status='open'",
                                  (answer or cmd, at, rid))
        if not claimed.rowcount:
            return False
        if cmd == 'answer':
            answer_scope = 'job:' + job['id']
            if scope == 'workflow' and req['kind'] == 'attestation' and details.get('workflow'):
                answer_scope = f"workflow:{details['workflow']}:{job['company_norm']}"
            expires = date.today().isoformat() if re.search(r'authoriz|sponsor|start date|available to start', req['question'], re.I) else None
            db.conn.execute('INSERT OR REPLACE INTO scoped_answers VALUES(?,?,?,?,?,?)',
                            (answer_scope, req['question'].strip(), answer, profile_hash or '', expires, at))
        elif cmd == 'approve':
            db.conn.execute('INSERT OR REPLACE INTO application_approvals VALUES(?,?,?)',
                            (job['id'], details['fingerprint'], at))
        elif cmd == 'done':
            state = 'applied'
            db.conn.execute('UPDATE jobs SET applied_at=? WHERE id=?', (at, job['id']))
            db.conn.execute("UPDATE applications SET status='RECONCILED_SUBMITTED',finished_at=?,evidence_json=? "
                            "WHERE job_id=? AND status IN ('IN_PROGRESS','SUBMISSION_UNKNOWN')", (at, dumps({'human_receipt': answer}), job['id']))
            db.conn.execute("INSERT INTO applications(job_id,mode,status,started_at,finished_at,evidence_json) VALUES(?,'manual','SUBMITTED',?,?,?)",
                            (job['id'], at, at, dumps({'human_receipt': answer})))
        elif cmd == 'retry':
            db.conn.execute("UPDATE applications SET status='RECONCILED_NOT_SUBMITTED',finished_at=?,evidence_json=? "
                            "WHERE job_id=? AND status IN ('IN_PROGRESS','SUBMISSION_UNKNOWN')", (at, dumps({'human_reconciliation': answer}), job['id']))
        elif cmd == 'skip':
            state = 'skipped'
        if cmd == 'resume' and req['kind'] == 'eligibility':
            db.conn.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                            ('eligibility_review:' + job['id'], job['description_hash'] or ''))
        if cmd == 'resume' and req['kind'] == 'duplicate':
            db.conn.execute('UPDATE jobs SET duplicate_of=NULL WHERE id=?', (job['id'],))
        db.conn.execute('UPDATE jobs SET status=? WHERE id=?', (state, job['id']))
        db.conn.execute('INSERT INTO journal(job_id,at,kind,detail_json) VALUES(?,?,?,?)',
                        (job['id'], at, 'human_resolved', dumps({'request_id': rid, 'command': cmd, 'scope': scope, 'evidence': answer})))
    return True
