"""Public ATS job-board feeds (no login, no key). Each fetcher returns normalized job dicts."""
import time
import requests

from ..util import strip_html

UA = {"User-Agent": "jobhunt/0.1 (personal job search; contact: owner)"}


class SourceUnavailable(RuntimeError):
    pass


class Feed(list):
    def __init__(self, items=(), complete=True):
        super().__init__(items)
        self.complete = complete


def _payload(response, key=None):
    if response.status_code != 200:
        raise SourceUnavailable(f'HTTP {response.status_code}; not an empty job board')
    body = response.json()
    items = body.get(key) if key and isinstance(body, dict) else body
    if not isinstance(items, list):
        raise SourceUnavailable('Malformed/incomplete job feed')
    return items


def _metadata(source, slug, raw):
    if not isinstance(raw, dict):
        return {'source': source, 'board_slug': slug, 'raw': raw}
    return {'source': source, 'board_slug': slug, 'source_job_id': str(raw.get('id') or ''),
            'requisition_id': raw.get('requisition_id'), 'employment_type': raw.get('employmentType'),
            'compensation': raw.get('compensation') or raw.get('salaryRange'),
            'locations': raw.get('secondaryLocations') or [], 'raw': raw}


def _strings_or_null(raw, *keys):
    return all(raw.get(key) is None or isinstance(raw[key], str) for key in keys)



def _get(url, **kw):
    r = requests.get(url, headers=UA, timeout=30, **kw)
    return r


def greenhouse(slug: str, company: str | None = None) -> list[dict]:
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    out = []
    for j in _payload(r, 'jobs'):
        meta = _metadata('greenhouse', slug, j)
        if not isinstance(j, dict):
            out.append(meta)
            continue
        if not _strings_or_null(j, 'title', 'absolute_url', 'content') or not isinstance(j.get('location') or {}, dict):
            out.append({**meta, 'normalization_error': 'invalid_vendor_fields'})
            continue
        out.append({**meta, "company": company or slug, "title": j.get("title", ""),
                    "location": (j.get("location") or {}).get("name", ""), "url": j.get("absolute_url", ""),
                    "apply_url": j.get("absolute_url", ""), "description": strip_html(j.get("content", "")),
                    "posted_at": j.get("updated_at") or j.get("first_published")})
    return out


def lever(slug: str, company: str | None = None) -> list[dict]:
    r = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    out = []
    for j in _payload(r, None):
        meta = _metadata('lever', slug, j)
        if not isinstance(j, dict):
            out.append(meta)
            continue
        if (not _strings_or_null(j, 'text', 'hostedUrl', 'applyUrl', 'descriptionPlain', 'description') or
                not isinstance(j.get('categories') or {}, dict) or not isinstance(j.get('lists', []), list) or
                any(not isinstance(item, dict) or not _strings_or_null(item, 'content') for item in j.get('lists', []))):
            out.append({**meta, 'normalization_error': 'invalid_vendor_fields'})
            continue
        cat = j.get("categories") or {}
        meta["employment_type"] = cat.get("commitment")
        desc = (j.get("descriptionPlain") or strip_html(j.get("description", ""))) + "\n" + \
               "\n".join(strip_html(x.get("content", "")) for x in j.get("lists", []))
        out.append({**meta, "company": company or slug, "title": j.get("text", ""),
                    "location": cat.get("location", "") or "", "url": j.get("hostedUrl", ""),
                    "apply_url": j.get("applyUrl") or j.get("hostedUrl", ""), "description": desc,
                    "posted_at": str(j.get("createdAt", ""))})
    return out


def ashby(slug: str, company: str | None = None) -> list[dict]:
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
    out = []
    for j in _payload(r, 'jobs'):
        meta = _metadata('ashby', slug, j)
        if not isinstance(j, dict):
            out.append(meta)
            continue
        if not _strings_or_null(j, 'title', 'jobUrl', 'applyUrl', 'descriptionPlain', 'descriptionHtml'):
            out.append({**meta, 'normalization_error': 'invalid_vendor_fields'})
            continue
        if not meta['source_job_id']:
            meta['source_job_id'] = (j.get('jobUrl') or '').rstrip('/').split('/')[-1]
        out.append({**meta, "company": company or slug, "title": j.get("title", ""),
                    "location": j.get("location", "") or "", "url": j.get("jobUrl", ""),
                    "apply_url": j.get("applyUrl") or j.get("jobUrl", ""),
                    "description": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
                    "posted_at": j.get("publishedAt")})
    return out


def workday(tenant_cfg: str, company: str | None = None, search: str = "software engineer", limit: int = 20) -> list[dict]:
    """tenant_cfg = 'tenant|wdN|site' e.g. 'acme|wd5|External'. UNTESTED against live tenants -
    Workday tenants differ; verify on the first run."""
    tenant, wd, site = tenant_cfg.split("|")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    r = requests.post(f"{base}/wday/cxs/{tenant}/{site}/jobs", headers=UA, timeout=30,
                      json={"appliedFacets": {}, "limit": limit, "offset": 0, "searchText": search})
    out = Feed(complete=False)  # Bounded search pages are not a complete employer inventory.
    for j in _payload(r, 'jobPostings'):
        meta = _metadata('workday', tenant_cfg, j)
        if not isinstance(j, dict):
            out.append(meta)
            continue
        if not _strings_or_null(j, 'externalPath', 'title', 'locationsText') or not j.get('externalPath'):
            out.append({**meta, 'normalization_error': 'invalid_vendor_fields'})
            continue
        path = j.get("externalPath", "")
        meta["source_job_id"] = path
        desc = ""
        try:
            d = requests.get(f"{base}/wday/cxs/{tenant}/{site}{path}", headers=UA, timeout=30)
            if d.status_code == 200:
                desc = strip_html(d.json().get("jobPostingInfo", {}).get("jobDescription", ""))
            time.sleep(0.5)
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            pass
        out.append({**meta, "company": company or tenant, "title": j.get("title", ""),
                    "location": j.get("locationsText", ""), "url": f"{base}/{site}{path}",
                    "apply_url": f"{base}/{site}{path}", "description": desc, "posted_at": j.get("postedOn")})
    return out


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday}
