"""MODULE 3 - Filter: hard rules -> sponsor/E-Verify boost -> Gemini fit score."""
import json
import re

from .discover import sponsors
from .llm import ConfigurationError, LLMError, ModelUnavailable, QuotaExceeded, TemporaryUnavailable

US_ABBR = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
           "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
US_ABBR_RX = re.compile(r"(?:,|\()\s*(" + "|".join(US_ABBR) + r")\b")
US_WORDS_RX = re.compile(r"\b(united states|usa|u\.s\.a?\.?|us[- ]remote|remote[,\s-]*(us|usa|united states)|"
                         r"new york|san francisco|seattle|austin|boston|chicago|los angeles|california|texas|"
                         r"washington|massachusetts|new jersey|colorado|virginia|florida|georgia|illinois)\b", re.I)
NON_US_RX = re.compile(r"\b(canada|toronto|vancouver|montreal|india|bangalore|bengaluru|hyderabad|pune|mumbai|"
                       r"united kingdom|uk|london|ireland|dublin|germany|berlin|munich|france|paris|netherlands|"
                       r"amsterdam|spain|madrid|barcelona|poland|warsaw|singapore|australia|sydney|melbourne|"
                       r"israel|tel aviv|china|beijing|shanghai|japan|tokyo|korea|seoul|brazil|sao paulo|mexico|"
                       r"argentina|colombia|emea|apac|latam|europe|philippines|pakistan|nigeria|egypt|dubai|uae)\b", re.I)
CA_PROVINCE_RX = re.compile(r",\s*(ON|BC|QC|AB)\b")


def classify_location(loc: str) -> str:
    loc = loc or ""
    if US_ABBR_RX.search(loc) and not (NON_US_RX.search(loc) and not US_WORDS_RX.search(loc)):
        return "us"
    if US_WORDS_RX.search(loc):
        return "us"
    if NON_US_RX.search(loc) or CA_PROVINCE_RX.search(loc):
        return "non_us"
    return "unknown"


def required_years(desc: str) -> int:
    """Largest 'N+ years ... experience' in a sentence that is not preferred/bonus."""
    best = 0
    for sent in re.split(r"(?<=[.!?\n])\s+|\n", desc or ""):
        if re.search(r"preferred|nice to have|bonus|plus\b|ideally", sent, re.I):
            continue
        for m in re.finditer(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|to|–)?\s*(\d{1,2})?\s*\+?\s*years?", sent, re.I):
            if re.search(r"experience|professional|industry|working", sent, re.I):
                best = max(best, int(m.group(1)))
    return best


class Rules:
    def __init__(self, ctx):
        d, t = ctx.dealbreakers, ctx.titles
        c = lambda lst: [re.compile(p, re.I) for p in lst or []]
        self.nospons, self.citizen = c(d["no_sponsorship_patterns"]), c(d["citizenship_clearance_patterns"])
        self.pos, self.senior, self.excl = c(d["sponsorship_positive_patterns"]), c(d["senior_title_patterns"]), c(d["title_exclude_patterns"])
        self.max_years, self.unknown_loc = d["max_required_years"], d["unknown_location"]
        self.cond, self.min_overlap = c(t.get("conditional_titles")), t.get("conditional_min_skill_overlap", 3)
        skills = {x for v in ctx.profile.get("skills", {}).values() for x in v}
        self.skill_rx = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(x) + r"(?![A-Za-z0-9])", re.I) for x in skills]
        self.inc, self.qual, self.early, self.tech = c(t["include"]), c(t["qualified_titles"]), c(t["early_career_labels"]), c(t["technical_terms"])

    def skill_overlap(self, desc: str) -> int:
        return sum(1 for r in self.skill_rx if r.search(desc or ""))

    def title_ok(self, title: str, desc: str) -> bool:
        has_tech = lambda s: any(r.search(s) for r in self.tech)
        if any(r.search(title) for r in self.inc):
            return True
        if any(r.search(title) for r in self.cond) and self.skill_overlap(desc) >= self.min_overlap:
            return True
        if any(r.search(title) for r in self.qual) and has_tech(title):
            return True
        if any(r.search(title) for r in self.early) and (has_tech(title) or has_tech((desc or "")[:500])):
            return True
        return False

    def check(self, job: dict) -> tuple[bool, str | None]:
        title, desc = job.get("title") or "", job.get("description") or ""
        for r in self.excl:
            if r.search(title):
                return False, f"title_excluded:{r.pattern[:30]}"
        for r in self.senior:
            if r.search(title):
                return False, "senior_title"
        if not self.title_ok(title, desc):
            return False, "title_not_target"
        loc = classify_location(job.get("location") or "")
        if loc == "non_us" or (loc == "unknown" and self.unknown_loc == "drop"):
            return False, f"location:{loc}"
        for r in self.nospons:
            m = r.search(desc)
            if m:
                return False, f"no_sponsorship:{m.group(0)[:50]}"
        for r in self.citizen:
            m = r.search(desc)
            if m:
                return False, f"citizenship_or_clearance:{m.group(0)[:50]}"
        yrs = required_years(desc)
        if yrs > self.max_years:
            return False, f"years_required:{yrs}"
        return True, None

    def jd_positive(self, desc: str) -> bool:
        return any(r.search(desc or "") for r in self.pos)


SYSTEM = ("You are a strict recruiting-fit evaluator for an entry-level candidate. Output ONLY JSON. "
          "Never invent candidate experience. Do not penalize the candidate for needing visa sponsorship "
          "unless the posting says it will not sponsor.")

PROMPT = """CANDIDATE
{cand}

JOB
Company: {company}
Title: {title}
Location: {location}
Description (truncated):
{desc}

Return JSON with keys:
fit (integer 1-10; 10 = ideal entry-level match on skills AND level),
reasons (list of <=3 short strings),
missing_skills (list of <=6 concrete skills/tools the JD wants that the candidate lacks),
seniority_flag (true if the role truly needs >3 years or senior scope),
requires_citizenship_or_clearance (true/false),
sponsorship_signal ("negative" if posting refuses/limits sponsorship or requires work authorization without sponsorship, "positive" if it offers sponsorship, else "none"),
location_us (true/false/null if unclear)."""


def sponsor_boost(ctx, job: dict, rules: Rules) -> tuple[float, dict]:
    f = ctx.settings["filter"]
    info = {"tier": None, "everify": False, "jd_positive": rules.jd_positive(job.get("description"))}
    boost = 0.0
    info["tier"] = sponsors.tier_for(ctx.db, job["company_norm"], f["sponsor_tiers"])
    if info["tier"]:
        boost += f["sponsor_boost"][info["tier"]]
    info["everify"] = sponsors.is_everify(ctx.db, job["company_norm"])
    if info["everify"]:
        boost += f["everify_boost"]
    if info["jd_positive"]:
        boost += f["jd_positive_boost"]
    return boost, info


def run(ctx, llm, profile, limit: int | None = None) -> dict:
    db, f = ctx.db, ctx.settings["filter"]
    rules = Rules(ctx)
    stats = {"rejected": 0, "queued": 0, "gap": 0, "skipped": 0, "scored": 0,
             "quota_stop": False, "model_error": False, "service_unavailable": False}
    for job in db.q("SELECT * FROM jobs WHERE status='new'"):
        ok, reason = rules.check(job)
        if ok:
            db.set_job(job["id"], status="passed_rules")
        else:
            db.set_job(job["id"], status="rejected", reject_reason=reason)
            stats["rejected"] += 1
    cand = profile.llm_summary()
    pending = db.q("SELECT * FROM jobs WHERE status='passed_rules'")
    # Complete descriptions first; sponsor evidence breaks ties.
    for j in pending:
        j["_boost"], j["_info"] = sponsor_boost(ctx, j, rules)
        j["_thin"] = len((j.get("description") or "").strip()) < f.get("thin_description_chars", 100)
    pending.sort(key=lambda j: (j["_thin"], -j["_boost"], j["discovered_at"] or ""))
    for job in pending[: limit or len(pending)]:
        try:
            desc = (job["description"] or "")[:6000]
            if job["_thin"]:
                desc = (desc or "Description unavailable.") + ("\n\nThis listing has limited details. Score only from the supplied "
                    "title, location and text. Do not infer missing requirements, skills, or sponsorship policy.")
            r = llm.json(SYSTEM, PROMPT.format(cand=cand, company=job["company"], title=job["title"],
                                               location=job["location"], desc=desc))
            fit = float(max(1, min(10, int(r.get("fit", 0)))))
        except QuotaExceeded:
            stats["quota_stop"] = True
            break
        except ModelUnavailable as e:
            print(f"[filter] {e}; stopping until the model is updated")
            stats["model_error"] = True
            break
        except ConfigurationError as e:
            print(f"[filter] {e}; stopping until configuration is fixed")
            stats["model_error"] = True
            break
        except TemporaryUnavailable as e:
            print(f"[filter] {e}; stopping without changing pending jobs")
            stats["service_unavailable"] = True
            break
        except (LLMError, ValueError, TypeError) as e:
            print(f"[filter] score failed for {job['id']}: {e}")
            continue
        stats["scored"] += 1
        info = job["_info"]
        r = dict(r)
        r["tentative"] = job["_thin"]
        fields = dict(sponsor_tier=info["tier"], everify=int(info["everify"]), jd_sponsor_positive=int(info["jd_positive"]),
                      fit_raw=fit, boost=job["_boost"], fit_json=json.dumps(r), missing_skills=json.dumps(r.get("missing_skills", [])))
        if r.get("seniority_flag") or r.get("requires_citizenship_or_clearance") \
                or r.get("sponsorship_signal") == "negative" or r.get("location_us") is False:
            why = ("seniority" if r.get("seniority_flag") else "citizenship/clearance" if r.get("requires_citizenship_or_clearance")
                   else "sponsorship_negative" if r.get("sponsorship_signal") == "negative" else "non_us")
            db.set_job(job["id"], status="rejected", reject_reason=f"llm:{why}", **fields)
            stats["rejected"] += 1
            continue
        final = min(10.0, fit + (job["_boost"] if f["boost_mode"] == "add" else 0))
        threshold = f.get("thin_apply_threshold", 9) if job["_thin"] else f["apply_threshold"]
        valid_url = (job.get("apply_url") or job.get("url") or "").startswith(("https://", "http://"))
        if final >= threshold and valid_url:
            status = "queued"
        elif final >= f["gap_log_min"]:
            status = "gap"
        else:
            status = "skipped"
        db.set_job(job["id"], status=status, fit_final=final, **fields)
        stats["queued" if status == "queued" else "gap" if status == "gap" else "skipped"] += 1
    return stats
