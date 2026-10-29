"""MODULE 4 - Tailor. LLM may select/reorder/reword; structure + facts stay locked.
Guards: every bullet must cite a real source id; numbers must exist in the source bullet; known tech names
not present anywhere in your facts are rejected. Failing bullets fall back to your original wording."""
import html
import json
import re
from pathlib import Path

from .llm import LLMError, QuotaExceeded

# Known tech names whose appearance must be backed by your facts. Ambiguous English words (go, rust, swift, spark) omitted on purpose.
LEXICON = set("""aws gcp azure kubernetes k8s terraform ansible jenkins kafka rabbitmq redis mongodb cassandra dynamodb
snowflake databricks airflow dbt hadoop pyspark tensorflow keras jax langchain llamaindex pinecone elasticsearch
graphql grpc golang scala kotlin ruby rails django flask spring springboot angular vue svelte nginx
cicd bigquery redshift sagemaker lambda ec2 openai cuda triton vllm mlflow kubeflow datadog grafana prometheus
swiftui flutter redux tailwind mongo oracle cobol php laravel dotnet .net c# rust""".split()) - {"rust"}

NUM = re.compile(r"\d+(?:\.\d+)?")
TOK = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]*")


def violations(new: str, source: str, allowed_tokens: set[str]) -> list[str]:
    v = []
    extra = set(NUM.findall(new)) - set(NUM.findall(source))
    if extra:
        v.append(f"numbers not in source: {sorted(extra)}")
    for t in set(TOK.findall(new)):
        tl = t.lower().strip(".-")
        if tl in LEXICON and tl not in allowed_tokens:
            v.append(f"tech not in your facts: {t}")
    return v


SYSTEM = ("You tailor resumes. You may ONLY select, reorder and lightly reword the provided bullets to emphasize "
          "relevance to the job. Never add tools, employers, metrics, numbers, scope, seniority or claims that are not "
          "in the source bullet you cite. Keep each bullet to one line-ish sentence. Output ONLY JSON.")

PROMPT = """JOB: {title} at {company}
JD (truncated):
{jd}

SOURCE BULLETS (id: text):
{bullets}

SKILLS (category: items): {skills}

Return JSON:
{{"experience":[{{"id":"<experience id>","bullets":[{{"src":"<bullet id>","text":"<reworded bullet>"}}]}}],
 "projects":[{{"id":"<project id>","bullets":[{{"src":"<bullet id>","text":"<reworded bullet>"}}]}}],
 "skills":{{"<category>":["<only items from SKILLS, most relevant first>"]}}{summary_key}}}
Rules: keep ALL experience ids; choose and order up to {max_projects} projects by relevance; each kept item needs >=1 bullet;
every bullet cites src from SOURCE BULLETS; reword only to mirror JD vocabulary that is TRUE of the source."""


def _fallback(profile) -> dict:
    p = profile.p
    return {
        "experience": [{"id": e["id"], "bullets": [{"src": b["id"], "text": b["text"]} for b in e["bullets"]]} for e in p["experience"]],
        "projects": [{"id": x["id"], "bullets": [{"src": b["id"], "text": b["text"]} for b in x["bullets"]]} for x in p["projects"]],
        "skills": {k: list(v) for k, v in p["skills"].items()},
    }


def build_plan(ctx, llm, profile, job: dict) -> tuple[dict, list[str]]:
    """Returns (validated plan, notes). Never raises on model misbehavior - falls back to locked text."""
    t = ctx.settings["tailor"]
    src = profile.bullets()
    allowed = {w.lower().strip(".-") for w in TOK.findall(profile.all_fact_text())}
    plan, notes = _fallback(profile), []
    bl = "\n".join(f"{k}: {v}" for k, v in src.items())
    sk = json.dumps(profile.p["skills"])
    try:
        raw = llm.json(SYSTEM, PROMPT.format(title=job["title"], company=job["company"], jd=(job["description"] or "")[:5000],
                                             bullets=bl, skills=sk, max_projects=t["max_projects"],
                                             summary_key=', "summary": "<=2 sentences, facts only>"' if t["include_summary"] else ""))
    except (QuotaExceeded, LLMError) as e:
        return plan, [f"llm unavailable, used locked text: {e}"]

    def clean(items, valid_ids, keep_all_ids=None):
        out = []
        for it in items or []:
            if it.get("id") not in valid_ids:
                continue
            bl2 = []
            for b in it.get("bullets", []):
                sid = b.get("src")
                if sid not in src:
                    notes.append(f"dropped bullet with bad src {sid}")
                    continue
                text = str(b.get("text", "")).strip()
                bad = violations(text, src[sid], allowed)
                if bad or not text:
                    notes.append(f"{sid}: reverted ({'; '.join(bad) or 'empty'})")
                    text = src[sid]
                bl2.append({"src": sid, "text": text})
            if bl2:
                out.append({"id": it["id"], "bullets": bl2})
        return out

    exp_ids = {e["id"] for e in profile.p["experience"]}
    proj_ids = {x["id"] for x in profile.p["projects"]}
    exp = clean(raw.get("experience"), exp_ids)
    got = {e["id"] for e in exp}
    for e in plan["experience"]:           # experience entries are never dropped
        if e["id"] not in got:
            exp.append(e)
    order = [e["id"] for e in profile.p["experience"]]
    plan["experience"] = sorted(exp, key=lambda e: order.index(e["id"]))
    proj = clean(raw.get("projects"), proj_ids)[: t["max_projects"]]
    if proj:
        plan["projects"] = proj
    skills = {}
    flat = profile.skills_flat()
    for cat, items in (raw.get("skills") or {}).items():
        if cat in profile.p["skills"]:
            keep = [i for i in items if str(i).lower() in flat and i in profile.p["skills"][cat]]
            keep += [i for i in profile.p["skills"][cat] if i not in keep]   # never drop skills silently
            skills[cat] = keep
    if skills:
        for cat in profile.p["skills"]:
            skills.setdefault(cat, profile.p["skills"][cat])
        plan["skills"] = skills
    if t["include_summary"] and raw.get("summary"):
        s = str(raw["summary"])
        if not violations(s, profile.all_fact_text(), allowed):
            plan["summary"] = s
        else:
            notes.append("summary rejected by fact check")
    return plan, notes


CSS = """body{font:10.5pt/1.25 Arial,Helvetica,sans-serif;margin:0.5in;color:#000}h1{font-size:18pt;margin:0;text-align:center}
.c{text-align:center;font-size:9.5pt;margin-bottom:6px}h2{font-size:11pt;border-bottom:1px solid #000;margin:10px 0 4px;text-transform:uppercase}
.r{display:flex;justify-content:space-between}ul{margin:2px 0 4px 18px;padding:0}li{margin:0}"""


def render_html(profile, plan: dict) -> str:
    p, E = profile.p, html.escape
    c = p["candidate"]
    L = c["links"]
    out = [f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>",
           f"<h1>{E(c['name'])}</h1><div class='c'>{E(c['location'])} | {E(c['phone'])} | {E(c['email'])}<br>"
           f"{E(L['linkedin'])} | {E(L['github'])} | {E(L['portfolio'])}</div>"]
    if plan.get("summary"):
        out.append(f"<h2>Summary</h2><div>{E(plan['summary'])}</div>")
    out.append("<h2>Education</h2>")
    for e in p["education"]:
        out.append(f"<div class='r'><b>{E(e['school'])} | {E(e['degree'])}</b><span>{E(e['date'])}</span></div>")
        out += [f"<div>{E(d)}</div>" for d in e.get("details", [])]
    out.append("<h2>Technical Skills</h2>")
    for cat, items in plan["skills"].items():
        out.append(f"<div><b>{E(cat)}:</b> {E(', '.join(items))}</div>")
    out.append("<h2>Experience</h2>")
    emap = {e["id"]: e for e in p["experience"]}
    for it in plan["experience"]:
        e = emap[it["id"]]
        out.append(f"<div class='r'><b>{E(e['title'])} | {E(e['company'])}</b><span>{E(e['dates'])}</span></div>")
        out.append("<ul>" + "".join(f"<li>{E(b['text'])}</li>" for b in it["bullets"]) + "</ul>")
    out.append("<h2>Projects</h2>")
    pmap = {x["id"]: x for x in p["projects"]}
    for it in plan["projects"]:
        x = pmap[it["id"]]
        out.append(f"<div><b>{E(x['name'])}</b> | {E(x['subtitle'])}</div>")
        out.append("<ul>" + "".join(f"<li>{E(b['text'])}</li>" for b in it["bullets"]) + "</ul>")
    out.append("</body></html>")
    return "".join(out)


def html_to_pdf(html_str: str, pdf_path: Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        pg.set_content(html_str)
        pg.pdf(path=str(pdf_path), format="Letter", print_background=True)
        b.close()
    return True


CL_SYSTEM = ("You write concise, honest cover letters for an entry-level candidate. Use ONLY the facts provided. "
             "No invented experience, numbers, tools or years. Do not mention visa status. Plain text, no placeholders.")
CL_PROMPT = """Write a cover letter (max {n} words, 3 short paragraphs, no 'Dear Hiring Manager' filler beyond one greeting line).
JOB: {title} at {company}
JD (truncated): {jd}
CANDIDATE FACTS:
{facts}
Sign off as {name}."""


def cover_letter(ctx, llm, profile, job: dict) -> tuple[str, list[str]]:
    n = ctx.settings["tailor"]["cover_letter_max_words"]
    allowed = {w.lower().strip(".-") for w in TOK.findall(profile.all_fact_text())}
    src = profile.all_fact_text()
    try:
        text = llm.text(CL_SYSTEM, CL_PROMPT.format(n=n, title=job["title"], company=job["company"],
                                                     jd=(job["description"] or "")[:4000], facts=profile.llm_summary(),
                                                     name=profile.p["candidate"]["name"]))
    except (QuotaExceeded, LLMError) as e:
        return "", [f"cover letter skipped: {e}"]
    bad = violations(text, src + " " + job["company"] + " " + job["title"], allowed)
    # numbers in a letter must exist in facts; company/title numbers allowed via source concat
    if bad or len(text.split()) > n * 1.3:
        return "", [f"cover letter rejected: {'; '.join(bad) or 'too long'}"]
    return text, []


def run(ctx, llm, profile, limit: int | None = None) -> dict:
    db, stats = ctx.db, {"tailored": 0, "notes": 0, "pdf": 0}
    for job in db.q("SELECT * FROM jobs WHERE status='queued' ORDER BY fit_final DESC")[: limit or 10**6]:
        d = ctx.out_dir / job["id"]
        d.mkdir(parents=True, exist_ok=True)
        plan, notes = build_plan(ctx, llm, profile, job)
        h = render_html(profile, plan)
        (d / "resume.html").write_text(h)
        base = profile.p["candidate"]["name"].split()
        fname = f"{base[0]}_{base[-1]}_Resume"
        pdf = d / f"{fname}.pdf"
        resume_path = str(pdf) if html_to_pdf(h, pdf) else str(d / "resume.html")
        stats["pdf"] += resume_path.endswith(".pdf")
        cl, n2 = cover_letter(ctx, llm, profile, job)
        cover_path = None
        if cl:
            cp = d / "cover_letter.md"
            cp.write_text(cl)
            cover_path = str(cp)
        (d / "tailor_notes.json").write_text(json.dumps(notes + n2, indent=2))
        db.set_job(job["id"], status="tailored", resume_path=resume_path, cover_path=cover_path)
        stats["tailored"] += 1
        stats["notes"] += len(notes + n2)
    return stats
