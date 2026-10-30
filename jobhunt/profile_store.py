"""MODULE 1 - Profile Store: locked facts, answer bank, dealbreakers, preflight."""
import re
import yaml
from datetime import date

from .util import deep_get
from .db import fingerprint

REQUIRED_FOR_APPLY = [
    "work_authorization.authorized_to_work_us_now",
    "work_authorization.earliest_start_date",
    "preferences.salary_answer_text",
    "preferences.willing_to_relocate",
]
OPTIONAL_TODO = ["work_authorization.current_status", "work_authorization.opt_start_date"]


class Profile:
    def __init__(self, ctx):
        self.ctx = ctx
        self.p = ctx.profile
        self.bank = ctx.answer_bank
        self.db = ctx.db

    def fingerprint(self):
        return fingerprint({'profile': self.p, 'answer_bank': self.bank})

    def reviewed(self):
        from .config import load_yaml
        try:
            on_disk = fingerprint({'profile': load_yaml('profile.yaml', self.ctx.root),
                                   'answer_bank': load_yaml('answer_bank.yaml', self.ctx.root)})
        except (OSError, ValueError, yaml.YAMLError):
            return False
        return self.db.kv_get('reviewed_profile_hash') == self.fingerprint() == on_disk

    def identity_missing(self):
        return [key for key in ('candidate.name', 'candidate.email', 'candidate.phone')
                if deep_get(self.p, key) in (None, '')]

    def validate(self):
        """Validate the facts/schema before accepting an operator profile review."""
        for key in ('candidate', 'work_authorization', 'preferences', 'skills'):
            if not isinstance(self.p.get(key), dict):
                raise ValueError(f'Profile {key} must be a mapping')
        if any(not isinstance(self.p['candidate'].get(key), str) or not self.p['candidate'][key].strip()
               for key in ('name', 'email', 'phone', 'location')):
            raise ValueError('Candidate name/email/phone/location must be nonempty strings')
        if not isinstance(self.p['candidate'].get('links'), dict) or any(
                not isinstance(self.p['candidate']['links'].get(key), str) for key in ('linkedin', 'github', 'portfolio')):
            raise ValueError('Candidate links must contain linkedin/github/portfolio strings (empty is allowed)')
        for items in self.p['skills'].values():
            if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                raise ValueError('Skill categories must contain lists of strings')
        seen = set()
        for section, keys in {'education': ('school', 'degree', 'date'),
                              'experience': ('id', 'company', 'title', 'dates'),
                              'projects': ('id', 'name', 'subtitle')}.items():
            if not isinstance(self.p.get(section), list):
                raise ValueError(f'{section} must be a list')
            for item in self.p[section]:
                if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip() for key in keys):
                    raise ValueError(f'{section} entries need nonempty strings: {keys}')
                if section != 'education':
                    if item['id'] in seen or not isinstance(item.get('bullets'), list):
                        raise ValueError('Experience/project IDs must be unique and bullets must be lists')
                    seen.add(item['id'])
        self.bullets()
        for answer in self.bank.get('answers', []):
            if not isinstance(answer, dict) or not isinstance(answer.get('name'), str) or not isinstance(answer.get('patterns'), list):
                raise ValueError('Answer-bank entries need names and pattern lists')
            for pattern in answer['patterns']:
                try:
                    re.compile(pattern)
                except (re.error, TypeError) as error:
                    raise ValueError('Invalid answer-bank question pattern') from error

    def authorization_confirmed(self):
        w = self.p.get('work_authorization', {})
        try:
            return date.fromisoformat(w['answers_confirmed_on']) <= date.today() <= date.fromisoformat(w['answers_valid_until'])
        except (KeyError, ValueError, TypeError):
            return False

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
                    if not isinstance(b, dict) or not isinstance(b.get('id'), str) or not isinstance(b.get('text'), str) or not b['text'].strip():
                        raise ValueError('Every source bullet needs a string ID and nonempty text')
                    if b['id'] in out:
                        raise ValueError('Bullet source IDs must be globally unique')
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
        prefs = p.get('preferences', {})
        lines.append('Seeking: ' + str(prefs.get('target_roles', 'Not specified')))
        lines.append('Employment types: ' + str(prefs.get('employment_types', 'Not specified')))
        lines.append('Location preferences: ' + str(prefs.get('locations', 'Not specified')))
        lines.append('Target base compensation (flexible): ' + str(prefs.get('target_base_usd', 'Not specified')))
        need = p.get('work_authorization', {}).get('needs_h1b_sponsorship')
        lines.append('Future employer sponsorship required: ' + ('Unknown' if need is None else str(need)))
        return "\n".join(lines)

    # ---- answer bank
    def _ref_value(self, ref: str):
        v = deep_get(self.p, ref)
        if isinstance(v, bool):
            return "Yes" if v else "No"
        return v

    def needs_human(self, question: str) -> bool:
        mandatory = r'certify|attest|agree|consent|acknowledge|signature|privacy|terms|background.check|criminal|drug.test|non.compete|veteran|disabilit|gender|race|ethnic|hispanic|pronoun'
        return bool(re.search(mandatory, question, re.I)) or any(re.search(p, question, re.I) for p in self.bank.get("always_human_patterns", []))

    def answer(self, question: str, job=None):
        """Returns (answer|None, source). None => must ask the human."""
        q = question.strip()
        scope = 'job:' + job['id'] if job else 'unscoped'
        row = self.db.one('SELECT * FROM scoped_answers WHERE scope=? AND question=? AND profile_hash=?',
                          (scope, q, self.fingerprint()))
        if not row and job and job.get('_workflow'):
            row = self.db.one('SELECT * FROM scoped_answers WHERE scope=? AND question=? AND profile_hash=?',
                              (f"workflow:{job['_workflow']}:{job['company_norm']}", q, self.fingerprint()))
        learned = row['answer'] if row and (not row['expires_at'] or row['expires_at'] >= date.today().isoformat()) else None
        if self.needs_human(q):
            return (learned, "learned") if learned else (None, "attestation")
        if learned:
            return learned, 'learned'
        if re.search(r'authoriz|sponsor|employment visa|start date|when can you start|available to start', q, re.I) and not self.authorization_confirmed():
            return None, 'authorization_unconfirmed'
        for a in self.bank.get("answers", []):
            if any(re.search(p, q, re.I) for p in a["patterns"]):
                v = self._ref_value(a["ref"]) if "ref" in a else a.get("answer")
                if v not in (None, ""):
                    return str(v), f"bank:{a['name']}"
                break
        return (learned, "learned") if learned else (None, "unknown")

    def answers_payload(self, job=None) -> dict:
        """Static, safe-to-share answers handed to the apply agent."""
        out = {}
        for a in self.bank.get("answers", []):
            if a['name'] in {'sponsorship', 'work_authorization', 'start_date'} and not self.authorization_confirmed():
                continue
            v = self._ref_value(a["ref"]) if "ref" in a else a.get("answer")
            if v not in (None, ""):
                out[a["name"]] = str(v)
        out["full_name"] = self.p["candidate"]["name"]
        prefs = self.p.get("preferences", {})
        if prefs.get("salary_min_usd") is not None and prefs.get("salary_max_usd") is not None:
            out["salary_min_usd"] = prefs["salary_min_usd"]
            out["salary_max_usd"] = prefs["salary_max_usd"]
        learned = {r['question']: r['answer'] for r in self.db.q(
            'SELECT * FROM scoped_answers WHERE scope=? AND profile_hash=? AND (expires_at IS NULL OR expires_at>=?)',
            ('job:' + job['id'] if job else 'unscoped', self.fingerprint(), date.today().isoformat()))}
        if learned:
            out["learned_answers_by_question"] = learned
        return out
