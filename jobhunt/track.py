"""MODULE 6 - Track: SQLite is the source of truth; Gmail (read-only, metadata+snippet) -> events; daily digest."""
import csv
import re
from email.utils import parseaddr

from .util import norm_company, now_iso, today

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


def match_job(db, sender: str, subject: str, snippet: str = "") -> str | None:
    name, addr = parseaddr(sender)
    domain = addr.split("@")[-1].split(".")[0].lower() if "@" in addr else ""
    hay = norm_company(f"{name} {subject} {snippet}")
    best, best_len = None, 0
    for j in db.q("SELECT id, company_norm FROM jobs WHERE status IN ('applied','interviewing','assessment')"):
        cn = j["company_norm"]
        if cn and (re.search(rf"\b{re.escape(cn)}\b", hay) or cn.replace(" ", "") == domain) and len(cn) > best_len:
            best, best_len = j["id"], len(cn)
    return best


def ingest(db, msgs: list[dict]) -> int:
    """msgs: {id, from, subject, snippet, date}"""
    n = 0
    for m in msgs:
        if db.one("SELECT 1 FROM events WHERE gmail_msg_id=?", (m["id"],)):
            continue
        kind = classify_email(m["subject"], m.get("snippet", ""))
        if not kind:
            continue
        jid = match_job(db, m["from"], m["subject"], m.get("snippet", ""))
        db.x("INSERT INTO events(job_id,kind,subject,sender,received_at,gmail_msg_id) VALUES(?,?,?,?,?,?)",
             (jid, kind, m["subject"][:200], m["from"][:200], m.get("date", now_iso()), m["id"]))
        if jid and kind in STATUS_FOR:
            db.set_job(jid, status=STATUS_FOR[kind])
        n += 1
    return n


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
    ids = svc.users().messages().list(userId="me", q=f"newer_than:{days}d -category:promotions", maxResults=max_results).execute().get("messages", [])
    out = []
    for i in ids:
        m = svc.users().messages().get(userId="me", id=i["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"]).execute()
        h = {x["name"]: x["value"] for x in m["payload"]["headers"]}
        out.append({"id": i["id"], "from": h.get("From", ""), "subject": h.get("Subject", ""), "snippet": m.get("snippet", ""), "date": h.get("Date", "")})
    return out


def export_csv(db, path) -> int:
    rows = db.q("SELECT company,title,location,source,status,fit_final,sponsor_tier,applied_at,apply_url FROM jobs "
                "WHERE status NOT IN ('new','rejected','skipped') ORDER BY fit_final DESC")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["company"])
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def digest(db) -> str:
    c = lambda s, a=(): db.one(s, a)["n"]
    lines = [f"jobhunt digest {today()}",
             f"Queued (7+): {c('SELECT COUNT(*) n FROM jobs WHERE status=?', ('queued',))} | tailored: {c('SELECT COUNT(*) n FROM jobs WHERE status=?', ('tailored',))} | "
             f"applied total: {c('SELECT COUNT(*) n FROM jobs WHERE status=?', ('applied',))}",
             f"Applied last 24h: {c('SELECT COUNT(*) n FROM jobs WHERE applied_at>=?', (today(),))} | dry runs ok: {db.kv_get('dry_runs_ok', '0')}"]
    nh = db.q("SELECT id, question FROM human_requests WHERE status='open'")
    if nh:
        lines.append("WAITING ON YOU: " + "; ".join(f"#{r['id']} {r['question'][:60]}" for r in nh))
    ev = db.q("SELECT e.kind, j.company, j.title FROM events e LEFT JOIN jobs j ON j.id=e.job_id WHERE e.received_at>=? ORDER BY e.id DESC", (today(),))
    for e in ev[:10]:
        lines.append(f"  {e['kind'].upper()}: {e['company'] or '?'} - {e['title'] or ''}")
    tot = {r["kind"]: r["n"] for r in db.q("SELECT kind, COUNT(*) n FROM events GROUP BY kind")}
    if tot:
        lines.append("Responses so far: " + ", ".join(f"{k}={v}" for k, v in tot.items()))
    return "\n".join(lines)
