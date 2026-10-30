"""Optional open-source scraper + Apify no-login actor adapters. Both are OFF unless configured.
LinkedIn is excluded by default (settings.discover.scraper_sites)."""
import os
from ..util import now_iso


def _cell(value) -> str:
    """Normalize missing values produced by pandas before inserting them into SQLite."""
    text = str(value).strip() if value is not None else ""
    return "" if text.lower() in ("nan", "none", "null", "nat", "<na>") else text


def jobspy(queries: list[str], location: str, sites: list[str], n: int, hours_old: int, db=None) -> list[dict]:
    def health(state, detail, fetched=0):
        if db:
            db.x('INSERT INTO source_runs(source,at,state,detail,fetched) VALUES(?,?,?,?,?)',
                 ('jobspy', now_iso(), state, str(detail)[:500], fetched))
    try:
        from jobspy import scrape_jobs  # pip install python-jobspy
    except ImportError:
        print("[discover] python-jobspy not installed; skipping scrapers")
        health("unavailable", "Optional python-jobspy not installed")
        return []
    out = []
    for q in queries:
        try:
            df = scrape_jobs(site_name=sites, search_term=q, location=location, results_wanted=n,
                             hours_old=hours_old, country_indeed="USA")
        except Exception as e:  # scrapers break often; never kill the run
            print(f"[discover] jobspy '{q}' failed: {e}")
            health("failed", type(e).__name__)
            continue
        errors, warnings = df.attrs.get('errors', {}), df.attrs.get('warnings', {})
        health('partial' if errors or warnings else 'ok', {'errors': errors, 'warnings': warnings}, len(df))
        for _, r in df.iterrows():
            url = _cell(r.get("job_url"))
            out.append({"source": f"jobspy:{_cell(r.get('site'))}", "company": _cell(r.get("company")),
                        "title": _cell(r.get("title")), "location": _cell(r.get("location")),
                        "url": url, "apply_url": _cell(r.get("job_url_direct")) or url,
                        "description": _cell(r.get("description")), "posted_at": _cell(r.get("date_posted")),
                        'employment_type': _cell(r.get('job_type')), 'source_job_id': _cell(r.get('id')),
                        'compensation': {k: _cell(r.get(k)) for k in ('min_amount', 'max_amount', 'interval', 'currency')},
                        'source_url': url})
    return out


def apify(run_input: dict, mapping: dict) -> list[dict]:
    """Needs APIFY_TOKEN + APIFY_ACTOR_ID (a no-login actor YOU choose) and a field mapping
    {company,title,location,url,description} -> dataset field names. Pip: apify-client."""
    tok, actor = os.environ.get("APIFY_TOKEN"), os.environ.get("APIFY_ACTOR_ID")
    if not (tok and actor):
        return []
    from apify_client import ApifyClient
    c = ApifyClient(tok)
    run = c.actor(actor).call(run_input=run_input)
    out = []
    for it in c.dataset(run["defaultDatasetId"]).iterate_items():
        out.append({"source": "apify", **{k: str(it.get(v, "")) for k, v in mapping.items()},
                    "apply_url": str(it.get(mapping.get("url", "url"), ""))})
    return out
