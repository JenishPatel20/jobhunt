"""Apply backends. Contract: apply(job, answers, dry_run) -> Result.
status: SUBMITTED | DRY_RUN_READY | NEEDS_HUMAN | FAILED | SUBMISSION_UNKNOWN
NEEDS_HUMAN carries kind (question|attestation|captcha|login|other) + text."""
import json
import re
from dataclasses import dataclass, field


@dataclass
class Result:
    status: str
    detail: str = ""
    kind: str = ""
    text: str = ""
    raw: dict = field(default_factory=dict)


def parse_final(text: str) -> Result:
    cands = re.findall(r"\{[^{}]*\"status\"[^{}]*\}", text or "", re.S)
    if not cands:
        return Result("FAILED", "no status JSON in agent output", raw={"tail": (text or "")[-500:]})
    try:
        d = json.loads(cands[-1])
    except json.JSONDecodeError:
        return Result("FAILED", "unparseable status JSON")
    st = str(d.get("status", "FAILED")).upper()
    if st not in {"SUBMITTED", "DRY_RUN_READY", "NEEDS_HUMAN", "FAILED"}:
        st = "FAILED"
    return Result(st, d.get("detail", ""), d.get("kind", ""), d.get("text", ""), d)


class GeminiCLIBackend:
    """Compatibility shim: legacy model-controlled browser submission is disabled."""
    def __init__(self, settings):
        pass

    def apply(self, job, answers, dry_run):
        return Result('FAILED', 'Legacy Gemini browser automation is disabled; use the guarded Playwright backend.')


class ApplyPilotBackend:
    """Disabled compatibility shim for the unverified external submitter."""

    def __init__(self, settings):
        pass

    def apply(self, job, answers, dry_run):
        return Result('FAILED', 'Unverified external ApplyPilot submission is disabled; use Playwright with confirmation evidence.')


class FakeBackend:
    """For tests / wiring checks. script: list of Results returned in order."""

    def __init__(self, script=None):
        self.script, self.calls = list(script or []), []

    def apply(self, job, answers, dry_run):
        self.calls.append((job["id"], dry_run))
        if self.script:
            return self.script.pop(0)
        return Result("DRY_RUN_READY" if dry_run else "SUBMITTED", "fake", raw={"confirmation": {"kind": "test", "text": "Fixture receipt"}})


def make_backend(settings, ctx=None, profile=None):
    name = settings["apply"]["backend"]
    from .browser import PlaywrightBackend
    return {"playwright": lambda s: PlaywrightBackend(s, ctx, profile), "gemini_cli": GeminiCLIBackend, "applypilot": ApplyPilotBackend, "fake": lambda s: DisabledBackend()}[name](settings)


class DisabledBackend:
    def apply(self, job, answers, dry_run):
        return Result('FAILED', 'FakeBackend is only available by explicit injection in offline tests')
