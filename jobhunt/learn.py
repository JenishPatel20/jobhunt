"""MODULE 7 - Refresh/Learn. Produces DRAFTS only. Nothing is ever posted anywhere.
LinkedIn: drafts are files + a ping; you copy/paste after approving. No LinkedIn automation."""
import json
import os
from collections import Counter
from pathlib import Path

import requests

from .llm import LLMError, QuotaExceeded
from .tailor import TOK, violations
from .util import today

SYS = "You write factual professional copy. Use ONLY the provided facts. Never invent tools, numbers, employers or claims."


def _month_dir(ctx, sub: str) -> Path:
    d = ctx.root / sub / today()[:7]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _checked(llm, profile, prompt, extra_source="") -> tuple[str, list[str]]:
    src = profile.all_fact_text() + "\n" + extra_source
    allowed = {w.lower().strip(".-") for w in TOK.findall(src)}
    text = llm.text(SYS, prompt)
    bad = violations(text, src, allowed)
    return text, bad


def refresh(ctx, llm, profile, notifier) -> list[str]:
    """Monthly drafts: GitHub READMEs, portfolio copy, LinkedIn headline/about."""
    out_dir, made = _month_dir(ctx, "drafts"), []
    facts = profile.llm_summary()
    user = ctx.profile["preferences"].get("github_username")
    # 1) GitHub READMEs (public repos via API; token optional)
    if user:
        hdr = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {}
        try:
            repos = requests.get(f"https://api.github.com/users/{user}/repos?sort=pushed&per_page=10", headers=hdr, timeout=20).json()
        except requests.RequestException:
            repos = []
        for r in repos if isinstance(repos, list) else []:
            if r.get("fork"):
                continue
            rd = requests.get(f"https://api.github.com/repos/{user}/{r['name']}/readme", headers={**hdr, "Accept": "application/vnd.github.raw+json"}, timeout=20)
            cur = rd.text[:6000] if rd.status_code == 200 else "(no README)"
            try:
                text, bad = _checked(llm, profile, f"Rewrite this repo README to be clear for recruiters (what it does, stack, how to run, results). "
                                     f"Keep every technical claim from the current README; add none.\nCURRENT README:\n{cur}\nCANDIDATE FACTS:\n{facts}", cur)
            except (QuotaExceeded, LLMError):
                break
            f = out_dir / f"github_readme_{r['name']}.md"
            f.write_text(("<!-- FACT-CHECK FLAGS: " + "; ".join(bad) + " -->\n" if bad else "") + text)
            made.append(str(f))
    # 2) portfolio + LinkedIn (never auto-posted)
    jobs = {
        "portfolio_projects_copy.md": "Write concise project card copy (2 sentences each) for every project in the facts, for a personal portfolio site.",
        "linkedin_headline_and_about.md": "Write 3 LinkedIn headline options (<=120 chars) and an About section (<=1500 chars) for a new-grad software/ML engineer. Do not mention visa status.",
        "linkedin_experience_bullets.md": "Rewrite the experience and project bullets for LinkedIn (same facts, slightly more narrative).",
    }
    for fname, ask in jobs.items():
        try:
            text, bad = _checked(llm, profile, f"{ask}\nFACTS:\n{facts}")
        except (QuotaExceeded, LLMError):
            break
        banner = "<!-- DRAFT. NOT POSTED. Review and copy manually. -->\n" + ("<!-- FACT-CHECK FLAGS: " + "; ".join(bad) + " -->\n" if bad else "")
        (out_dir / fname).write_text(banner + text)
        made.append(str(out_dir / fname))
    notifier.send(f"Monthly refresh drafts ready ({len(made)} files) in {out_dir}. LinkedIn items need your approval - nothing was posted.", subject="jobhunt: monthly drafts")
    return made


def skill_gaps(db, top: int = 12) -> list[tuple[str, int]]:
    """Frequency of missing skills, weighting the 5-6 'gap' band x2 (those jobs were nearly good enough)."""
    c = Counter()
    for r in db.q("SELECT status, missing_skills FROM jobs WHERE missing_skills IS NOT NULL AND status NOT IN ('new')"):
        w = 2 if r["status"] == "gap" else 1
        for s in json.loads(r["missing_skills"] or "[]"):
            c[str(s).strip().lower()] += w
    return c.most_common(top)


def learning_plan(ctx, llm, db) -> str:
    gaps = skill_gaps(db)
    if not gaps:
        return "No skill-gap data yet (needs scored jobs)."
    hrs = ctx.settings["learn"]["weekly_learning_hours"]
    prompt = (f"A new-grad software/ML engineer has {hrs} hours/week. Their most frequent skill gaps from real job postings "
              f"(skill, weighted count): {gaps}. Build a 4-week plan: pick the top 2-3 gaps, give weekly goals, a small "
              f"portfolio deliverable per gap, and name well-known free courses or official certifications by NAME ONLY. "
              f"No URLs, no prices, no invented course titles - say 'search for official <vendor> docs/tutorial' if unsure.")
    try:
        body = llm.text("You are a pragmatic engineering mentor.", prompt)
    except (QuotaExceeded, LLMError) as e:
        body = f"(LLM unavailable: {e})"
    out = "# Skill-gap report " + today() + "\n\n" + "\n".join(f"- {s}: {n}" for s, n in gaps) + "\n\n" + body + \
          "\n\n_Verify course names/certs exist and are current before committing time._\n"
    (_month_dir(ctx, "reports") / "skill_gap_plan.md").write_text(out)
    return out
