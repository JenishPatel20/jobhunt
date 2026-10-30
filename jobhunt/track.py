"""MODULE 6 - Track: SQLite is the source of truth; Gmail (read-only, metadata+snippet) -> events; daily digest."""
import csv
import re
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from email.utils import parseaddr

from .util import norm_company, now_iso, today, day_start_utc

RULES = [  # order matters: first match wins
    ("offer", r"offer letter|pleased to offer|extend (you )?an offer|offer of employment"),
    ("rejection", r"unfortunately|not (be )?moving forward|other candidates|decided not to (proceed|move)|regret to inform|will not be (moving|proceeding)|no longer (being )?considered"),
    ("assessment", r"assessment|coding challenge|hackerrank|codesignal|codility|take-?home|online test"),
    ("interview", r"schedule (a|an|your)? ?(call|interview|conversation|chat)|phone screen|interview|next steps|your availability|calendly"),
    ("received", r"thank(s| you) for applying|application (has been )?received|we('ve| have) received your application|thanks for your interest"),
]
STATUS_FOR = {"offer": "offer", "rejection": "rejected_by_company", "assessment": "assessment", "interview": "interviewing"}


def classify_email(subject: str, snippet: str = "") -> str | None:
    t = f"{subject}\n{snippet}".lower()
    for kind, rx in RULES:
        if re.search(rx, t):
            return kind
    return None


def match_candidates(db, sender, subject, snippet=''):
    name, addr = parseaddr(sender)
    domain = addr.split('@')[-1].split('.')[0].lower() if '@' in addr else ''
    hay = norm_company(f'{name} {subject} {snippet}')
    candidates = []
    for job in db.q("SELECT * FROM jobs WHERE applied_at IS NOT NULL OR status IN ('applied','interviewing','assessment')"):
        company = job['company_norm']
        if company and (re.search(rf'\b{re.escape(company)}\b', hay) or company.replace(' ', '') == domain):
            candidates.append(job)
    explicit = []
    for job in candidates:
        metadata = json.loads(job.get('metadata_json') or '{}')
        ids = [job.get('source_job_id'), metadata.get('requisition_id')]
        title = re.sub(r'\s+', ' ', job['title']).casefold()
        if any(value and len(str(value)) >= 4 and re.search(r'(?<![\w-])' + re.escape(str(value)) + r'(?![\w-])', subject + ' ' + snippet, re.I) for value in ids) or title in (subject + ' ' + snippet).casefold():
            explicit.append(job)
    return explicit or candidates


def match_job(db, sender, subject, snippet=''):
    candidates = match_candidates(db, sender, subject, snippet)
    return candidates[0]['id'] if len(candidates) == 1 else None


def event_date(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, IndexError):
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat(timespec='seconds')


def apply_event(db, event):
    previous = [event_date(row['received_at']) for row in db.q('SELECT received_at FROM events WHERE job_id=? AND id<>?', (event['job_id'], event['id']))]
    latest = max((value for value in previous if value), default=None)
    if event['kind'] in STATUS_FOR and (not latest or event['received_at'] >= latest):
        db.set_job(event['job_id'], status=STATUS_FOR[event['kind']])


def ingest(db, msgs):
    n = 0
    for message in msgs:
        if db.one('SELECT 1 FROM events WHERE gmail_msg_id=?', (message['id'],)):
            continue
        kind = classify_email(message['subject'], message.get('snippet', ''))
        if not kind:
            continue
        candidates = match_candidates(db, message['from'], message['subject'], message.get('snippet', ''))
        received = event_date(message.get('date'))
        jid = candidates[0]['id'] if len(candidates) == 1 and received else None
        event = db.x('INSERT INTO events(job_id,kind,subject,sender,received_at,gmail_msg_id) VALUES(?,?,?,?,?,?)',
                     (jid, kind, message['subject'][:200], message['from'][:200], received or now_iso(), message['id']))
        if jid:
            apply_event(db, db.one('SELECT * FROM events WHERE id=?', (event.lastrowid,)))
        else:
            db.x('INSERT INTO email_reviews(event_id,candidates_json) VALUES(?,?)',
                 (event.lastrowid, json.dumps([job['id'] for job in candidates])))
            db.x('INSERT INTO outbox(text) VALUES(?)', (f'Email event #{event.lastrowid} needs a job match. Review with jobhunt email-reviews; resolve with jobhunt email-link EVENT_ID JOB_ID.',))
        n += 1
    return n


def link_event(db, event_id, job_id):
    event = db.one('SELECT * FROM events WHERE id=?', (event_id,))
    review = db.one("SELECT * FROM email_reviews WHERE event_id=? AND status='open'", (event_id,))
    if not event or not review or not db.one('SELECT id FROM jobs WHERE id=?', (job_id,)):
        return False
    with db.conn:
        db.conn.execute('UPDATE events SET job_id=? WHERE id=?', (job_id, event_id))
        db.conn.execute("UPDATE email_reviews SET status='resolved' WHERE event_id=?", (event_id,))
    db.record(job_id, 'email_linked_by_human', {'event_id': event_id})
    apply_event(db, db.one('SELECT * FROM events WHERE id=?', (event_id,)))
    return True


def gmail_fetch(root, days: int = 14, max_results: int = 100) -> list[dict]:
    """Read-only Gmail. Needs credentials.json (Google Cloud OAuth desktop client) in the project root.
    pip install google-api-python-client google-auth-oauthlib. Fetches headers + snippet only (no bodies)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    tok = root / "token.json"
    creds = Credentials.from_authorized_user_file(str(tok), scopes) if tok.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(str(root / "credentials.json"), scopes).run_local_server(port=0)
        tok.write_text(creds.to_json())
    svc = build("gmail", "v1", credentials=creds)
    ids, page_token = [], None
    while True:
        page = svc.users().messages().list(userId="me", q=f"newer_than:{days}d -category:promotions", maxResults=max_results, pageToken=page_token).execute()
        ids.extend(page.get('messages', []))
        page_token = page.get('nextPageToken')
        if not page_token:
            break
    out = []
    for i in ids:
        m = svc.users().messages().get(userId="me", id=i["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"]).execute()
        h = {x["name"]: x["value"] for x in m["payload"]["headers"]}
        out.append({"id": i["id"], "from": h.get("From", ""), "subject": h.get("Subject", ""), "snippet": m.get("snippet", ""), "date": h.get("Date", "")})
    return out


def export_csv(db, path):
    rows = db.q('SELECT id,company,title,location,source,status,fit_raw,fit_final,sponsor_tier,availability,discovered_at,last_seen_at,applied_at,apply_url,reject_reason FROM jobs ORDER BY discovered_at DESC')
    fields = list(rows[0]) if rows else ['id', 'company', 'title', 'status']
    with open(path, 'w', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v) for k, v in row.items()})
    return len(rows)


def digest(db):
    count = lambda sql, args=(): db.one(sql, args)['n']
    states = {r['status']: r['n'] for r in db.q('SELECT status,COUNT(*) n FROM jobs GROUP BY status')}
    lines = [f'jobhunt digest {today()}',
             f"Observed: {count('SELECT COUNT(*) n FROM observations')} | unique jobs: {count('SELECT COUNT(*) n FROM jobs')}",
             f"Shortlisted: {states.get('queued', 0)} | prepared: {states.get('tailored', 0)} | closed: {states.get('closed', 0)}",
             'Confirmed submitted total: ' + str(count("SELECT COUNT(DISTINCT job_id) n FROM applications WHERE status IN ('SUBMITTED','RECONCILED_SUBMITTED')")),
             'Confirmed submitted today: ' + str(count("SELECT COUNT(DISTINCT job_id) n FROM applications WHERE status IN ('SUBMITTED','RECONCILED_SUBMITTED') AND finished_at>=?", (day_start_utc(),))),
             'Uncertain submissions: ' + str(count("SELECT COUNT(*) n FROM applications WHERE status IN ('IN_PROGRESS','SUBMISSION_UNKNOWN')"))]
    for reason in db.q("SELECT reject_reason,COUNT(*) n FROM jobs WHERE status='rejected' GROUP BY reject_reason"):
        lines.append(f"Rejected {reason['reject_reason']}: {reason['n']}")
    for req in db.q("SELECT id,kind,question FROM human_requests WHERE status='open'"):
        lines.append(f"WAITING ON YOU #{req['id']} ({req['kind']}): {req['question'][:80]}")
    reviews = count("SELECT COUNT(*) n FROM email_reviews WHERE status='open'")
    if reviews:
        lines.append(f'Email matches awaiting review: {reviews}')
    failures = count("SELECT COUNT(*) n FROM source_runs WHERE state<>'ok' AND at>=?", (day_start_utc(),))
    lines.append(f'Source failures/partial runs today: {failures}')
    lines.append('Outcomes: ' + ', '.join(f'{s}={n}' for s, n in states.items() if s in {'interviewing', 'assessment', 'offer', 'rejected_by_company'}))
    return '\n'.join(lines)
