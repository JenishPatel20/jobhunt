import hashlib
import json
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
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
CREATE TABLE IF NOT EXISTS observations(
  id INTEGER PRIMARY KEY, job_id TEXT, observed_at TEXT NOT NULL, source TEXT,
  payload_json TEXT NOT NULL, disposition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_sources(
  url TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  observed_at TEXT, payload_json TEXT NOT NULL, UNIQUE(job_id,content_hash));
CREATE TABLE IF NOT EXISTS journal(
  id INTEGER PRIMARY KEY, job_id TEXT, at TEXT NOT NULL, kind TEXT NOT NULL,
  detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS availability_checks(
  id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, at TEXT NOT NULL, state TEXT NOT NULL,
  evidence_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_runs(
  id INTEGER PRIMARY KEY, source TEXT, at TEXT, state TEXT, detail TEXT, fetched INTEGER);
CREATE TABLE IF NOT EXISTS assessments(
  id INTEGER PRIMARY KEY, job_id TEXT, at TEXT, cache_key TEXT UNIQUE,
  result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(
  id INTEGER PRIMARY KEY, job_id TEXT, at TEXT, kind TEXT, path TEXT, sha256 TEXT,
  profile_hash TEXT, source_json TEXT, validation_json TEXT);
CREATE TABLE IF NOT EXISTS scoped_answers(
  scope TEXT, question TEXT, answer TEXT, profile_hash TEXT, expires_at TEXT,
  approved_at TEXT, PRIMARY KEY(scope,question));
CREATE TABLE IF NOT EXISTS workflow_approvals(
  workflow TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, at TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS application_approvals(
  job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, request_id INTEGER, text TEXT, state TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0, next_attempt TEXT, sent_at TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS telegram_updates(
  update_id INTEGER PRIMARY KEY, at TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS email_reviews(
  event_id INTEGER PRIMARY KEY, candidates_json TEXT, status TEXT DEFAULT 'open');
"""


def canonical_url(url):
    if not isinstance(url, str):
        return ''
    try:
        parts = urlsplit(url.strip())
        parts.port  # Validate malformed port/IPv6 syntax before using the URL.
    except ValueError:
        return ''
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        return ''
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'source', 'ref', 'referrer', 'trackingid'}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/') or '/',
                       urlencode(sorted(query)), ''))


def fingerprint(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


class DB:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA busy_timeout=5000')
        legacy = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        version = self.conn.execute('PRAGMA user_version').fetchone()[0]
        if legacy and version < 2:
            backup_path = self.path.with_name(self.path.name + '.before-v2.db')
            if not backup_path.exists():
                backup = sqlite3.connect(backup_path)
                try:
                    self.conn.backup(backup)
                finally:
                    backup.close()
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)
        self.migrate()
        self.repair_job_values()

    def migrate(self):
        """Additive, restart-safe migration; retain historical IDs and application states."""
        if self.conn.execute('PRAGMA user_version').fetchone()[0] >= 2:
            return
        additions = {
            'jobs': {'source_job_id': 'TEXT', 'board_slug': 'TEXT', 'identity_key': 'TEXT',
                     'last_seen_at': 'TEXT', 'description_hash': 'TEXT', 'metadata_json': "TEXT DEFAULT '{}'",
                     'availability': "TEXT DEFAULT 'uncertain'", 'availability_at': 'TEXT',
                     'eligibility_json': "TEXT DEFAULT '{}'", 'duplicate_of': 'TEXT',
                     'cover_required': 'INTEGER DEFAULT 0', 'score_key': 'TEXT',
                     'documents_valid': 'INTEGER DEFAULT 0', 'document_profile_hash': 'TEXT'},
            'applications': {'workflow': 'TEXT', 'provenance_json': "TEXT DEFAULT '{}'",
                             'evidence_json': "TEXT DEFAULT '{}'", 'submit_started_at': 'TEXT'},
            'human_requests': {'details_json': "TEXT DEFAULT '{}'", 'last_notified_at': 'TEXT'},
        }
        with self.conn:
            for table, fields in additions.items():
                existing = {r['name'] for r in self.conn.execute(f'PRAGMA table_info({table})')}
                for field, declaration in fields.items():
                    if field not in existing:
                        self.conn.execute(f'ALTER TABLE {table} ADD COLUMN {field} {declaration}')
            for row in self.conn.execute('SELECT id,url,apply_url,source,discovered_at FROM jobs').fetchall():
                # Retire the old company/title/location unique key without rebuilding the table.
                self.conn.execute('UPDATE jobs SET dedupe_key=?,last_seen_at=coalesce(last_seen_at,discovered_at) WHERE id=?',
                                  ('job:' + row['id'], row['id']))
                for url in {canonical_url(row['url']), canonical_url(row['apply_url'])} - {''}:
                    self.conn.execute('INSERT OR IGNORE INTO job_sources VALUES(?,?,?,?,?)',
                                      (url, row['id'], row['source'], row['discovered_at'], row['discovered_at']))
            self.conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS job_identity ON jobs(identity_key) WHERE identity_key IS NOT NULL')
            self.conn.execute('PRAGMA user_version=2')

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
        """Persist every observation; only merge exact source identity or canonical URL."""
        payload = dumps(j)
        if not isinstance(j, dict):
            j = {}
        url = canonical_url(j.get('apply_url') or j.get('url'))
        title = str(j.get('title') or '').strip()
        invalid_types = any(j.get(key) is not None and not isinstance(j[key], str)
                            for key in ('title', 'company', 'location', 'description', 'source', 'board_slug'))
        if not url or not title or invalid_types:
            self.x('INSERT INTO observations(job_id,observed_at,source,payload_json,disposition) VALUES(NULL,?,?,?,?)',
                   (now_iso(), str(j.get('source') or ''), payload, 'invalid:field_types' if invalid_types else 'invalid:missing_title_or_http_url'))
            return False
        source = str(j.get('source') or 'unknown')
        identity = (f"{source}:{j.get('board_slug', '')}:{j['source_job_id']}"
                    if j.get('source_job_id') and j.get('board_slug') else None)
        existing = self.one('SELECT * FROM jobs WHERE identity_key=?', (identity,)) if identity else None
        alias = self.one('SELECT job_id FROM job_sources WHERE url=?', (url,))
        if not existing and alias:
            candidate = self.one('SELECT * FROM jobs WHERE id=?', (alias['job_id'],))
            if not identity or not candidate['identity_key'] or candidate['identity_key'] == identity:
                existing = candidate
        jid = existing['id'] if existing else self.job_id(identity or url)
        cn, at = norm_company(str(j.get('company') or '')), now_iso()
        desc = str(j.get('description') or '')
        dh = fingerprint(desc)
        with self.conn:
            if not existing:
                duplicate = self.one('SELECT id FROM jobs WHERE company_norm=? AND lower(title)=? AND coalesce(location,\'\')=? LIMIT 1',
                                     (cn, title.lower(), j.get('location') or ''))
                self.conn.execute('''INSERT INTO jobs(id,dedupe_key,source,company,company_norm,title,location,url,apply_url,
                    description,posted_at,discovered_at,source_job_id,board_slug,identity_key,last_seen_at,
                    description_hash,metadata_json,duplicate_of) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (jid, 'job:' + jid, source, j.get('company'), cn, title, j.get('location'), j.get('url') or url,
                     url, desc, j.get('posted_at'), at, j.get('source_job_id'), j.get('board_slug'), identity, at,
                     dh, payload, duplicate['id'] if duplicate else None))
                self.conn.execute('INSERT INTO journal(job_id,at,kind,detail_json) VALUES(?,?,?,?)',
                                  (jid, at, 'discovered', dumps({'duplicate_of': duplicate['id'] if duplicate else None})))
            else:
                # Empty observations must not erase a previously complete description.
                changed = bool((desc and fingerprint(existing['description'] or '') != dh) or
                               title != existing['title'] or (j.get('location') and j.get('location') != existing['location']))
                self.conn.execute('UPDATE jobs SET last_seen_at=?,identity_key=coalesce(identity_key,?), '
                                  'source_job_id=coalesce(source_job_id,?),board_slug=coalesce(board_slug,?), '
                                  'metadata_json=? WHERE id=?',
                                  (at, identity, j.get('source_job_id'), j.get('board_slug'), payload, jid))
                if changed:
                    desc = desc or existing['description'] or ''
                    dh = fingerprint(desc)
                    self.conn.execute('UPDATE jobs SET title=?,location=coalesce(?,location) WHERE id=?', (title, j.get('location'), jid))
                    self.conn.execute('UPDATE jobs SET description=?,description_hash=?,score_key=NULL,documents_valid=0 WHERE id=?',
                                      (desc, dh, jid))
                    uncertain_attempt = self.one("SELECT id FROM applications WHERE job_id=? AND status IN ('IN_PROGRESS','SUBMISSION_UNKNOWN')", (jid,))
                    if existing['status'] in {'new', 'passed_rules', 'queued', 'gap', 'skipped', 'rejected', 'tailored', 'dry_run_ok', 'document_blocked', 'closed'} or (existing['status'] == 'needs_human' and not uncertain_attempt):
                        self.conn.execute("UPDATE jobs SET status='new' WHERE id=?", (jid,))
                        self.conn.execute("UPDATE human_requests SET status='stale',resolved_at=? WHERE job_id=? AND status='open' AND kind<>'interrupted'", (at, jid))
                    self.conn.execute('DELETE FROM application_approvals WHERE job_id=?', (jid,))
                    self.conn.execute('INSERT INTO journal(job_id,at,kind,detail_json) VALUES(?,?,?,?)',
                                      (jid, at, 'description_changed', dumps({'hash': dh})))
            for link in {url, canonical_url(j.get('url')), canonical_url(j.get('source_url'))} - {''}:
                self.conn.execute('INSERT INTO job_sources VALUES(?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET last_seen=excluded.last_seen',
                                  (link, jid, source, at, at))
            self.conn.execute('INSERT INTO observations(job_id,observed_at,source,payload_json,disposition) VALUES(?,?,?,?,?)',
                              (jid, at, source, payload, 'seen' if existing else 'new'))
            self.conn.execute('INSERT OR IGNORE INTO snapshots(job_id,content_hash,observed_at,payload_json) VALUES(?,?,?,?)',
                              (jid, fingerprint(j), at, payload))
        return not existing

    def set_job(self, job_id: str, **fields):
        if not fields:
            return
        allowed = {r['name'] for r in self.conn.execute('PRAGMA table_info(jobs)')}
        if fields.keys() - allowed:
            raise ValueError('Unknown job field')
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
            self.conn.execute('INSERT INTO journal(job_id,at,kind,detail_json) VALUES(?,?,?,?)',
                              (job_id, now_iso(), 'job_updated', dumps(fields)))

    def record(self, job_id, kind, detail):
        self.x('INSERT INTO journal(job_id,at,kind,detail_json) VALUES(?,?,?,?)',
               (job_id, now_iso(), kind, dumps(detail)))

    def claim(self, job_id, mode, workflow, provenance):
        with self.conn:
            cur = self.conn.execute("UPDATE jobs SET status='applying' WHERE id=? AND status='tailored'", (job_id,))
            if not cur.rowcount:
                return None
            attempt = self.conn.execute('INSERT INTO applications(job_id,mode,status,started_at,workflow,provenance_json) '
                                        "VALUES(?,?,'IN_PROGRESS',?,?,?)",
                                        (job_id, mode, now_iso(), workflow, dumps(provenance)))
            self.conn.execute('INSERT INTO journal(job_id,at,kind,detail_json) VALUES(?,?,?,?)',
                              (job_id, now_iso(), 'attempt_started', dumps({'attempt_id': attempt.lastrowid, 'mode': mode})))
        return attempt.lastrowid

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
    return json.dumps(o, ensure_ascii=False, sort_keys=True, default=str)
