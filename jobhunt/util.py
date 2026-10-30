import html
import re
import datetime as dt

_SUFFIX = {"inc", "llc", "corp", "corporation", "ltd", "limited", "co", "company", "technologies",
           "technology", "holdings", "group", "the", "lp", "plc", "incorporated", "usa", "us", "labs"}


def norm_company(name: str) -> str:
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(t for t in s.split() if t not in _SUFFIX)


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h[1-6])\s*/?>", "\n", s)
    s = re.sub(r"(?i)<\s*li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return dt.date.today().isoformat()


def day_start_utc() -> str:
    """UTC instant for midnight in the operating machine's local timezone."""
    return dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc).isoformat(timespec='seconds')


def deep_get(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur
