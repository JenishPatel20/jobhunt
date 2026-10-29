"""Public ATS job-board feeds (no login, no key). Each fetcher returns normalized job dicts."""
import time
import requests

from ..util import strip_html

UA = {"User-Agent": "jobhunt/0.1 (personal job search; contact: owner)"}


def _get(url, **kw):
    r = requests.get(url, headers=UA, timeout=30, **kw)
    return r


def greenhouse(slug: str, company: str | None = None) -> list[dict]:
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        out.append({"source": "greenhouse", "company": company or slug, "title": j.get("title", ""),
                    "location": (j.get("location") or {}).get("name", ""), "url": j.get("absolute_url", ""),
                    "apply_url": j.get("absolute_url", ""), "description": strip_html(j.get("content", "")),
                    "posted_at": j.get("updated_at") or j.get("first_published")})
    return out


def lever(slug: str, company: str | None = None) -> list[dict]:
    r = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if r.status_code != 200:
        return []
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        desc = (j.get("descriptionPlain") or strip_html(j.get("description", ""))) + "\n" + \
               "\n".join(strip_html(x.get("content", "")) for x in j.get("lists", []))
        out.append({"source": "lever", "company": company or slug, "title": j.get("text", ""),
                    "location": cat.get("location", "") or "", "url": j.get("hostedUrl", ""),
                    "apply_url": j.get("applyUrl") or j.get("hostedUrl", ""), "description": desc,
                    "posted_at": str(j.get("createdAt", ""))})
    return out


def ashby(slug: str, company: str | None = None) -> list[dict]:
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        out.append({"source": "ashby", "company": company or slug, "title": j.get("title", ""),
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
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobPostings", []):
        path = j.get("externalPath", "")
        desc = ""
        try:
            d = requests.get(f"{base}/wday/cxs/{tenant}/{site}{path}", headers=UA, timeout=30)
            if d.status_code == 200:
                desc = strip_html(d.json().get("jobPostingInfo", {}).get("jobDescription", ""))
            time.sleep(0.5)
        except requests.RequestException:
            pass
        out.append({"source": "workday", "company": company or tenant, "title": j.get("title", ""),
                    "location": j.get("locationsText", ""), "url": f"{base}/{site}{path}",
                    "apply_url": f"{base}/{site}{path}", "description": desc, "posted_at": j.get("postedOn")})
    return out


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday}
