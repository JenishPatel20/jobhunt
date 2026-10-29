"""H-1B sponsor whitelist from DOL LCA disclosure data (or USCIS H-1B Employer Data Hub export).
YOU download the file (free) and run: jobhunt sponsors-import <file.csv|xlsx>.
Column detection is alias-based; untested against every fiscal-year layout - the importer fails loudly if it can't find an employer column.
E-Verify has no bulk public list I can rely on: maintain config/everify.txt (one company per line)."""
import csv
import re
from collections import defaultdict
from pathlib import Path

from ..util import norm_company

EMP = ["employer_name", "employer", "employer (petitioner) name", "employer_business_name"]
STATUS = ["case_status"]
VISA = ["visa_class"]
TITLE = ["job_title", "soc_title"]
FY = ["fiscal year", "fiscal_year"]
USCIS_APPR = ["initial approval", "continuing approval"]
TECH_RX = re.compile(r"software|computer|data|machine learning|\bml\b|\bai\b|developer|programmer|engineer", re.I)


def _rows(path: Path):
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        header = [str(h or "").strip().lower() for h in next(it)]
        for row in it:
            yield dict(zip(header, row))
    else:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            rd = csv.DictReader(f)
            rd.fieldnames = [(h or "").strip().lower() for h in rd.fieldnames or []]
            for row in rd:
                yield row


def _first(row, keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def import_file(db, path: str, label: str | None = None) -> dict:
    p = Path(path)
    agg = defaultdict(lambda: {"name": "", "all": 0, "tech": 0, "fy": ""})
    n = 0
    for row in _rows(p):
        name = _first(row, EMP)
        if not name:
            continue
        n += 1
        key = norm_company(str(name))
        if not key:
            continue
        a = agg[key]
        a["name"] = a["name"] or str(name).title()
        if _first(row, USCIS_APPR[:1]) is not None or _first(row, USCIS_APPR[1:]) is not None:
            # USCIS Data Hub row: counts of approvals
            cnt = 0
            for k in USCIS_APPR:
                try:
                    cnt += int(str(row.get(k) or 0).replace(",", ""))
                except ValueError:
                    pass
            a["all"] += cnt
        else:
            status = str(_first(row, STATUS) or "").upper()
            visa = str(_first(row, VISA) or "H-1B").upper()
            if "CERTIFIED" not in status or "H-1B" not in visa:
                continue
            a["all"] += 1
            if TECH_RX.search(str(_first(row, TITLE) or "")):
                a["tech"] += 1
        a["fy"] = str(_first(row, FY) or a["fy"])
    if n == 0:
        raise SystemExit("No employer column found. Expected one of: " + ", ".join(EMP))
    for key, a in agg.items():
        if a["all"] == 0:
            continue
        db.x("""INSERT INTO sponsors(norm_name,display_name,h1b_certified,tech_certified,latest_fy,source)
                VALUES(?,?,?,?,?,?) ON CONFLICT(norm_name) DO UPDATE SET
                h1b_certified=h1b_certified+excluded.h1b_certified,
                tech_certified=tech_certified+excluded.tech_certified, latest_fy=excluded.latest_fy""",
             (key, a["name"], a["all"], a["tech"], a["fy"], label or p.name))
    return {"rows": n, "employers": len(agg)}


def load_everify(db, path: Path) -> int:
    if not path.exists():
        return 0
    c = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            db.x("INSERT OR IGNORE INTO everify(norm_name,display_name) VALUES(?,?)", (norm_company(line), line))
            c += 1
    return c


def tier_for(db, company_norm: str, tiers: dict) -> str | None:
    r = db.one("SELECT tech_certified t, h1b_certified a FROM sponsors WHERE norm_name=?", (company_norm,))
    if not r:
        return None
    n = r["t"] if r["t"] else 0
    for name in ("strong", "some", "weak"):
        if n >= tiers[name]:
            return name
    return "weak" if r["a"] else None


def is_everify(db, company_norm: str) -> bool:
    return bool(db.one("SELECT 1 FROM everify WHERE norm_name=?", (company_norm,)))
