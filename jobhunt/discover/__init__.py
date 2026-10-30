"""MODULE 2 - Discover."""
import datetime as dt
import re
import time

import requests

from . import ats, scrapers, sponsors
from ..util import norm_company, now_iso
from ..db import canonical_url


def _fresh(job: dict, max_age_days: int) -> bool:
    p = str(job.get("posted_at") or "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", p)
    if not m:
        return True  # unknown date -> keep
    try:
        d = dt.date(*map(int, m.groups()))
    except ValueError:
        return True
    return (dt.date.today() - d).days <= max_age_days


def probe_companies(ctx, top_n: int = 200, delay: float = 1.0) -> int:
    """Guess Greenhouse/Lever/Ashby slugs for top sponsors (cheap GETs). Marks verified only when
    Greenhouse returns a board name matching the sponsor. Review with: jobhunt companies"""
    db = ctx.db
    rows = db.q("SELECT norm_name, display_name FROM sponsors ORDER BY tech_certified DESC, h1b_certified DESC LIMIT ?", (top_n,))
    found = 0
    for r in rows:
        base = r["norm_name"]
        cands = {base.replace(" ", ""), base.replace(" ", "-")}
        for slug in cands:
            if not slug or db.one("SELECT 1 FROM companies WHERE slug=?", (slug,)):
                continue
            hit = None
            try:
                g = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}", headers=ats.UA, timeout=15)
                if g.status_code == 200:
                    nm = g.json().get("name", "")
                    hit = ("greenhouse", nm, int(norm_company(nm) == base))
                else:
                    for kind, url in (("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1"),
                                      ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}")):
                        x = requests.get(url, headers=ats.UA, timeout=15)
                        if x.status_code == 200:
                            hit = (kind, r["display_name"], 0)
                            break
                        time.sleep(0.3)
            except requests.RequestException:
                continue
            if hit:
                db.x("INSERT OR IGNORE INTO companies(norm_name,ats,slug,display_name,verified) VALUES(?,?,?,?,?)",
                     (base, hit[0], slug, hit[1] or r["display_name"], hit[2]))
                found += 1
            time.sleep(delay)
    return found


def run(ctx) -> dict:
    s, db = ctx.settings["discover"], ctx.db
    sponsors.load_everify(db, ctx.root / "config" / "everify.txt")
    max_age = ctx.settings["filter"]["max_age_days"]
    stats = {"fetched": 0, "new": 0, "boards": 0, "source_failures": 0}
    boards = [c for c in db.q("SELECT * FROM companies") if c["ats"] in s["boards"]]
    # manual list wins: config/companies.yaml entries are inserted as verified
    import yaml
    cf = ctx.root / "config" / "companies.yaml"
    if cf.exists():
        for c in (yaml.safe_load(cf.read_text()) or {}).get("companies", []):
            db.x("INSERT OR IGNORE INTO companies(norm_name,ats,slug,display_name,verified) VALUES(?,?,?,?,1)",
                 (norm_company(c["name"]), c["ats"], c["slug"], c["name"]))
        boards = [c for c in db.q("SELECT * FROM companies") if c["ats"] in s["boards"]]
    for c in boards:
        try:
            jobs = ats.FETCHERS[c["ats"]](c["slug"], c["display_name"])
        except Exception as e:
            print(f"[discover] {c['ats']}/{c['slug']} failed: {e}")
            db.x('INSERT INTO source_runs(source,at,state,detail,fetched) VALUES(?,?,?,?,?)',
                 (f"{c['ats']}/{c['slug']}", now_iso(), 'failed', type(e).__name__, 0))
            stats['source_failures'] += 1
            continue
        stats["boards"] += 1
        for j in jobs:
            stats["fetched"] += 1
            if db.upsert_job(j):
                stats["new"] += 1
                if not _fresh(j, max_age):
                    alias = db.one('SELECT job_id FROM job_sources WHERE url=?',
                                   (canonical_url(j.get('apply_url') or j.get('url')),))
                    if alias:
                        db.set_job(alias['job_id'], status='rejected', reject_reason='posting_age')
        db.x('INSERT INTO source_runs(source,at,state,detail,fetched) VALUES(?,?,?,?,?)',
             (f"{c['ats']}/{c['slug']}", now_iso(), 'ok' if getattr(jobs, 'complete', True) else 'partial',
              '' if getattr(jobs, 'complete', True) else 'Bounded/incomplete discovery; never infer closure from absence', len(jobs)))
        time.sleep(0.5)
    # optional scrapers
    qf = ctx.root / "config" / "queries.yaml"
    if qf.exists():
        q = yaml.safe_load(qf.read_text()) or {}
        for j in scrapers.jobspy(q.get("queries", []), q.get("location", "United States"),
                                 s["scraper_sites"], s["results_per_query"], s["hours_old"], db=db):
            stats["fetched"] += 1
            if db.upsert_job(j):
                stats["new"] += 1
    return stats
