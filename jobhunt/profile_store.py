"""MODULE 1 - Profile Store: locked facts, answer bank, dealbreakers, preflight."""
import re

from .util import deep_get

REQUIRED_FOR_APPLY = [
    "work_authorization.authorized_to_work_us_now",
    "work_authorization.earliest_start_date",
    "preferences.salary_answer_text",
    "preferences.willing_to_relocate",
]
OPTIONAL_TODO = ["work_authorization.current_status", "work_authorization.opt_start_date"]


class Profile:
    def __init__(self, ctx):
        self.p = ctx.profile
        self.bank = ctx.answer_bank
        self.db = ctx.db

    # ---- preflight
    def missing(self) -> tuple[list[str], list[str]]:
        req = [k for k in REQUIRED_FOR_APPLY if deep_get(self.p, k) in (None, "")]
        opt = [k for k in OPTIONAL_TODO if deep_get(self.p, k) in (None, "")]
        for pr in self.p.get("projects", []):
            if not pr.get("repo_url"):
                opt.append(f"projects.{pr['id']}.repo_url")
        return req, opt

    # ---- locked facts
    def bullets(self) -> dict[str, str]:
        out = {}
        for sec in ("experience", "projects"):
            for item in self.p.get(sec, []):
                for b in item.get("bullets", []):
                    out[b["id"]] = b["text"]
        return out

    def skills_flat(self) -> set[str]:
        return {s.lower() for v in self.p.get("skills", {}).values() for s in v}

    def all_fact_text(self) -> str:
        parts = [str(self.p.get("candidate", {}).get("name", ""))]
        for e in self.p.get("education", []):
            parts += [e["school"], e["degree"], *e.get("details", [])]
        for k, v in self.p.get("skills", {}).items():
            parts += [k, *v]
        for sec in ("experience", "projects"):
            for it in self.p.get(sec, []):
                parts += [it.get("company", ""), it.get("title", ""), it.get("name", ""), it.get("subtitle", "")]
                parts += [b["text"] for b in it.get("bullets", [])]
        return "\n".join(parts)

    def llm_summary(self) -> str:
        """Candidate summary for the LLM. Deliberately excludes phone/email/links."""
        p = self.p
        lines = [f"Education: " + "; ".join(f"{e['degree']} {e['school']} ({e['date']})" for e in p["education"])]
        lines.append("Skills: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in p["skills"].items()))
        for e in p["experience"]:
            lines.append(f"Experience: {e['title']} @ {e['company']} ({e['dates']})")
            lines += [f"  - {b['text']}" for b in e["bullets"]]
        for pr in p["projects"]:
            lines.append(f"Project: {pr['name']} - {pr['subtitle']}")
            lines += [f"  - {b['text']}" for b in pr["bullets"]]
        lines.append("Seeking: new-grad / entry-level full-time roles. Needs H-1B sponsorship long-term.")
        return "\n".join(lines)

    # ---- answer bank
    def _ref_value(self, ref: str):
        v = deep_get(self.p, ref)
        if isinstance(v, bool):
            return "Yes" if v else "No"
        return v

    def needs_human(self, question: str) -> bool:
        return any(re.search(p, question, re.I) for p in self.bank.get("always_human_patterns", []))

    def answer(self, question: str):
        """Returns (answer|None, source). None => must ask the human."""
        q = question.strip()
        learned = self.db.learned(q.lower())
        if self.needs_human(q):
            return (learned, "learned") if learned else (None, "attestation")
        for a in self.bank.get("answers", []):
            if any(re.search(p, q, re.I) for p in a["patterns"]):
                v = self._ref_value(a["ref"]) if "ref" in a else a.get("answer")
                if v not in (None, ""):
                    return str(v), f"bank:{a['name']}"
                break
        return (learned, "learned") if learned else (None, "unknown")

    def answers_payload(self) -> dict:
        """Static, safe-to-share answers handed to the apply agent."""
        out = {}
        for a in self.bank.get("answers", []):
            v = self._ref_value(a["ref"]) if "ref" in a else a.get("answer")
            if v not in (None, ""):
                out[a["name"]] = str(v)
        out["full_name"] = self.p["candidate"]["name"]
        prefs = self.p.get("preferences", {})
        if prefs.get("salary_min_usd") is not None and prefs.get("salary_max_usd") is not None:
            out["salary_min_usd"] = prefs["salary_min_usd"]
            out["salary_max_usd"] = prefs["salary_max_usd"]
        learned = {r["key"]: r["answer"] for r in self.db.q("SELECT key, answer FROM learned_answers")}
        if learned:
            out["learned_answers_by_question"] = learned
        return out
