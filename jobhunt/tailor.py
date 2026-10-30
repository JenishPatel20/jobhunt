"""MODULE 4 - Tailor. LLM may select/reorder/reword; structure + facts stay locked.
Guards: every bullet must cite a real source id; numbers must exist in the source bullet; known tech names
not present anywhere in your facts are rejected. Failing bullets fall back to your original wording."""
import html
import hashlib
import json
import re
import uuid
from html.parser import HTMLParser
from pathlib import Path

from .llm import LLMError, QuotaExceeded
from .db import dumps, fingerprint
from . import availability
from .util import now_iso

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

    if not isinstance(raw, dict):
        return plan, ['invalid model plan; used locked facts']
    owners = {it['id']: {b['id'] for b in it['bullets']} for sec in ('experience', 'projects') for it in profile.p[sec]}

    def clean(items, valid_ids, keep_all_ids=None):
        out = []
        seen = set()
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict) or not isinstance(it.get('id'), str) or it.get("id") not in valid_ids or it['id'] in seen:
                continue
            seen.add(it['id'])
            bl2 = []
            for b in it.get("bullets", []) if isinstance(it.get('bullets', []), list) else []:
                if not isinstance(b, dict):
                    continue
                sid = b.get("src")
                if not isinstance(sid, str) or sid not in owners[it['id']]:
                    notes.append(f"dropped bullet with bad src {sid}")
                    continue
                text = b.get('text', '').strip() if isinstance(b.get('text'), str) else ''
                source_tokens = {w.lower().strip('.-') for w in TOK.findall(src[sid])}
                bad = violations(text, src[sid], source_tokens)
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
    for cat, items in (raw.get("skills") if isinstance(raw.get('skills'), dict) else {}).items():
        if not isinstance(items, list):
            continue
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


CSS = """@page{size:Letter;margin:0.5in}*{box-sizing:border-box}body{font:10.5pt/1.25 Arial,Helvetica,sans-serif;margin:0;color:#000;overflow-wrap:anywhere}h1{font-size:18pt;margin:0;text-align:center}
.c{text-align:center;font-size:9.5pt;margin-bottom:6px}h2{font-size:11pt;border-bottom:1px solid #000;margin:10px 0 4px;text-transform:uppercase}
.r{display:flex;justify-content:space-between;gap:12px}.r span{flex-shrink:0}h2,.r{break-after:avoid}ul{margin:2px 0 4px 18px;padding:0}li{margin:0;break-inside:avoid}"""


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
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            try:
                pg = b.new_page(viewport={'width': 720, 'height': 1056})
                pg.set_content(html_str)
                pg.emulate_media(media='print')
                overflow = pg.evaluate('''() => [...document.body.querySelectorAll('*')].some(e => {
                    const r=e.getBoundingClientRect();return r.width && (r.left < -1 || r.right > 721 || e.scrollWidth > e.clientWidth+2);
                })''')
                if overflow:
                    return False
                pg.pdf(path=str(pdf_path), format="Letter", print_background=True, prefer_css_page_size=True)
            finally:
                b.close()
        return validate_pdf(pdf_path, html_str)['valid']
    except Exception:
        return False


def validate_pdf(path, html_str):
    """Check extraction/completeness; visual layout additionally uses DOM overflow checks."""
    class VisibleText(HTMLParser):
        def __init__(self):
            super().__init__()
            self.hidden = 0
            self.parts = []
        def handle_starttag(self, tag, attrs):
            if tag in {'head', 'style', 'script'}:
                self.hidden += 1
        def handle_endtag(self, tag):
            if tag in {'head', 'style', 'script'}:
                self.hidden -= 1
        def handle_data(self, data):
            if not self.hidden and data.strip():
                self.parts.append(data)
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        normalize = lambda text: re.sub(r'\s+', '', text).casefold()
        extracted = normalize(' '.join(page.extract_text() or '' for page in reader.pages))
        parser = VisibleText()
        parser.feed(html_str)
        missing = [part for part in parser.parts if normalize(part) not in extracted]
        return {'valid': bool(extracted) and not missing and 0 < len(reader.pages) <= 3,
                'pages': len(reader.pages), 'missing_fragments': len(missing)}
    except Exception as error:
        return {'valid': False, 'reason': type(error).__name__}


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
    if bad or len(text.split()) > n:
        return "", [f"cover letter rejected: {'; '.join(bad) or 'too long'}"]
    return text, []


def run(ctx, llm, profile, limit: int | None = None, checker=None) -> dict:
    db, stats = ctx.db, {"tailored": 0, "notes": 0, "pdf": 0, 'blocked': 0}
    for job in db.q("SELECT * FROM jobs WHERE status='queued' ORDER BY fit_final DESC,boost DESC")[: limit or 10**6]:
        result = availability.check(ctx, job, 'before_tailoring', checker)
        if result.state != 'open':
            if result.state == 'uncertain' and db.one('SELECT status FROM jobs WHERE id=?', (job['id'],))['status'] != 'new':
                from .apply import hitl
                from .notify import Notifier
                hitl.open_request(db, Notifier(db), job, 'availability', 'Could not confirm that the employer posting is open.',
                                  details={'resume_status': 'queued', 'evidence': result.evidence})
            stats['blocked'] += 1
            continue
        d = ctx.out_dir / job["id"] / uuid.uuid4().hex
        d.mkdir(parents=True, exist_ok=True)
        plan, notes = build_plan(ctx, llm, profile, job)
        if any(note.startswith('llm unavailable') for note in notes):
            stats['blocked'] += 1
            continue
        h = render_html(profile, plan)
        (d / "resume.html").write_text(h)
        base = profile.p["candidate"]["name"].split()
        fname = f"{base[0]}_{base[-1]}_Resume"
        pdf = d / f"{fname}.pdf"
        valid = html_to_pdf(h, pdf)
        resume_path = str(pdf) if valid else None
        stats["pdf"] += bool(valid)
        cl, n2 = cover_letter(ctx, llm, profile, job) if job['cover_required'] else ('', [])
        cover_path = None
        if cl:
            cp = d / 'cover_letter.pdf'
            letter_html = f'<html><head><style>{CSS}</style></head><body>' + ''.join(f'<p>{html.escape(p)}</p>' for p in cl.split('\n\n')) + '</body></html>'
            (d / 'cover_letter.html').write_text(letter_html)
            if html_to_pdf(letter_html, cp):
                cover_path = str(cp)
            else:
                valid = False
        if job['cover_required'] and not cover_path:
            valid = False
        (d / "tailor_notes.json").write_text(json.dumps(notes + n2, indent=2))
        (d / 'plan.json').write_text(dumps(plan))
        for kind, path in [('resume', resume_path), ('cover', cover_path)]:
            if path:
                db.x('INSERT INTO artifacts(job_id,at,kind,path,sha256,profile_hash,source_json,validation_json) VALUES(?,?,?,?,?,?,?,?)',
                     (job['id'], now_iso(), kind, path, hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                      profile.fingerprint(), dumps({'facts': profile.p, 'plan': plan, 'description_hash': job['description_hash']}),
                      dumps({'valid': valid, 'text_and_layout_checked': True})))
        db.set_job(job["id"], status="tailored" if valid else 'document_blocked', resume_path=resume_path,
                   cover_path=cover_path, documents_valid=int(valid), document_profile_hash=profile.fingerprint())
        stats['tailored' if valid else 'blocked'] += 1
        stats["notes"] += len(notes + n2)
    return stats
