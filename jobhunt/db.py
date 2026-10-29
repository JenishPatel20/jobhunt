import hashlib
import json
import sqlite3
from pathlib import Path

from .util import norm_company, now_iso, today

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE, source TEXT, company TEXT, company_norm TEXT,
  title TEXT, location TEXT, url TEXT, apply_url TEXT, description TEXT, posted_at TEXT,
  discovered_at TEXT, status TEXT DEFAULT 'new', reject_reason TEXT,
  sponsor_tier TEXT, everify INTEGER DEFAULT 0, jd_sponsor_positive INTEGER DEFAULT 0,
  fit_raw REAL, boost REAL, fit_final REAL, fit_json TEXT, missing_skills TEXT,
  resume_path TEXT, cover_path TEXT, applied_at TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS applications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, mode TEXT, status TEXT,
  started_at TEXT, finished_at TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, kind TEXT, subject TEXT, sender TEXT,
  received_at TEXT, gmail_msg_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS sponsors(
  norm_name TEXT PRIMARY KEY, display_name TEXT, h1b_certified INTEGER, tech_certified INTEGER,
  latest_fy TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS everify(norm_name TEXT PRIMARY KEY, display_name TEXT);
CREATE TABLE IF NOT EXISTS companies(
  norm_name TEXT, ats TEXT, slug TEXT, display_name TEXT, verified INTEGER DEFAULT 0,
  extra TEXT, PRIMARY KEY(ats, slug));
CREATE TABLE IF NOT EXISTS human_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, kind TEXT, question TEXT, answer TEXT,
  status TEXT DEFAULT 'open', created_at TEXT, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS learned_answers(key TEXT PRIMARY KEY, answer TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS llm_usage(day TEXT PRIMARY KEY, calls INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS llm_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, model TEXT, outcome TEXT,
  http_status INTEGER, latency_ms INTEGER
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.repair_job_values()

    def repair_job_values(self):
        """Repair legacy JobSpy null strings without changing stable job IDs or statuses."""
        self.conn.execute("UPDATE jobs SET description=NULL WHERE lower(trim(description)) IN ('nan','none','null','nat','<na>')")
        self.conn.execute("""UPDATE jobs SET apply_url=url
            WHERE lower(trim(coalesce(apply_url,''))) IN ('','nan','none','null','nat','<na>')
              AND (url LIKE 'https://%' OR url LIKE 'http://%')""")
        self.conn.commit()

    # --- generic helpers
    def q(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def x(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    def kv_get(self, k, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return r["v"] if r else default

    def kv_set(self, k, v):
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    # --- jobs
    @staticmethod
    def job_id(url: str) -> str:
        return hashlib.sha1(url.encode()).hexdigest()[:16]

    def upsert_job(self, j: dict) -> bool:
        """Insert if new (by URL hash or company+title+location). Returns True if inserted."""
        url = j.get("apply_url") or j.get("url") or ""
        if not url or not j.get("title"):
            return False
        cn = norm_company(j.get("company", ""))
        dk = f"{cn}|{j['title'].strip().lower()}|{(j.get('location') or '').strip().lower()}"
        jid = self.job_id(url)
        existing = self.one("SELECT id,description,apply_url FROM jobs WHERE id=? OR dedupe_key=?", (jid, dk))
        if existing:
            desc = (j.get("description") or "").strip()
            old = (existing["description"] or "").strip()
            if len(old) < 100 and len(desc) >= 100:
                self.set_job(existing["id"], description=desc)
            if (existing["apply_url"] or "").strip().lower() in ("", "nan", "none", "null"):
                self.set_job(existing["id"], apply_url=url)
            return False
        self.x(
            """INSERT INTO jobs(id,dedupe_key,source,company,company_norm,title,location,url,apply_url,
               description,posted_at,discovered_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'new')""",
            (jid, dk, j.get("source"), j.get("company"), cn, j["title"], j.get("location"),
             j.get("url") or url, url, j.get("description"), j.get("posted_at"), now_iso()))
        return True

    def set_job(self, job_id: str, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    # --- LLM quota
    def llm_calls_today(self) -> int:
        r = self.one("SELECT calls FROM llm_usage WHERE day=?", (today(),))
        return r["calls"] if r else 0

    def bump_llm(self):
        self.x("INSERT INTO llm_usage(day,calls) VALUES(?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1", (today(),))

    def record_llm_attempt(self, model, outcome, latency_ms, http_status=None):
        self.x("INSERT INTO llm_attempts(at,model,outcome,http_status,latency_ms) VALUES(?,?,?,?,?)",
               (now_iso(), model, outcome, http_status, int(latency_ms)))

    # --- misc
    def learned(self, key):
        r = self.one("SELECT answer FROM learned_answers WHERE key=?", (key,))
        return r["answer"] if r else None

    def learn(self, key, answer):
        self.x("INSERT INTO learned_answers(key,answer,created_at) VALUES(?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET answer=excluded.answer", (key, answer, now_iso()))


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)
