"""Bounded model providers; subscription-only execution has no API-key fallback."""
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .llm import ConfigurationError, LLMError, QuotaExceeded, TemporaryUnavailable, parse_json


class Provider(Protocol):
    def json(self, system: str, prompt: str): ...
    def text(self, system: str, prompt: str) -> str: ...
    def cooldown_until(self): ...


class CodexSubscription:
    """Use the supported CLI, verified ChatGPT sign-in, and isolated read-only runs."""
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings.get('llm', {})
        self.model = 'codex-subscription'

    def cooldown_until(self):
        raw = self.db.kv_get('llm_cooldown_until')
        return datetime.fromisoformat(raw) if raw else None

    def _call(self, system, prompt):
        until = self.cooldown_until()
        if until and until > datetime.now(timezone.utc):
            raise TemporaryUnavailable(f'Model work paused until {until.isoformat()}')
        if self.db.llm_calls_today() >= int(self.settings.get('daily_cap', 100)):
            raise QuotaExceeded('Local daily model-call cap reached')
        env = {k: v for k, v in os.environ.items() if k not in {'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ORGANIZATION', 'OPENAI_PROJECT'}}
        started = time.monotonic()
        try:
            auth = subprocess.run(['codex', 'login', 'status'], capture_output=True, text=True, timeout=15, env=env)
            if auth.returncode or 'chatgpt' not in (auth.stdout + auth.stderr).casefold():
                raise ConfigurationError('Use `codex login` with ChatGPT subscription access. API-key authentication is refused.')
            with tempfile.TemporaryDirectory(prefix='jobhunt-model-') as td:
                output = Path(td) / 'response.txt'
                cmd = ['codex', 'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
                       '--sandbox', 'read-only', '-c', 'model_provider="openai"',
                       '-c', 'features.shell_tool=false', '-c', 'web_search="disabled"',
                       '--output-last-message', str(output), '-']
                self.db.bump_llm()
                run = subprocess.run(cmd, input=system + '\n\nUse only the supplied data. Do not invoke tools.\n\n' + prompt,
                                     cwd=td, env=env, capture_output=True, text=True,
                                     timeout=int(self.settings.get('timeout_sec', 180)))
                if run.returncode:
                    until = datetime.now(timezone.utc) + timedelta(minutes=int(self.settings.get('cooldown_minutes', 30)))
                    self.db.kv_set('llm_cooldown_until', until.isoformat())
                    self.db.record_llm_attempt(self.model, 'unavailable', (time.monotonic() - started) * 1000)
                    raise TemporaryUnavailable('Codex failed or reached its limit; queue saved, no paid fallback')
                if not output.exists():
                    raise LLMError('Codex did not produce a final response')
                result = output.read_text().strip()
            self.db.record_llm_attempt(self.model, 'ok', (time.monotonic() - started) * 1000)
            return result
        except FileNotFoundError as error:
            raise ConfigurationError('Codex CLI is not installed; install it and sign in using your existing ChatGPT plan') from error
        except subprocess.TimeoutExpired as error:
            self.db.record_llm_attempt(self.model, 'timeout', (time.monotonic() - started) * 1000)
            until = datetime.now(timezone.utc) + timedelta(minutes=30)
            self.db.kv_set('llm_cooldown_until', until.isoformat())
            raise TemporaryUnavailable('Model timeout; no fallback request was made') from error

    def json(self, system, prompt):
        return parse_json(self._call(system, prompt))

    def text(self, system, prompt):
        return self._call(system, prompt)


def make_provider(db, settings):
    name = settings.get('llm', {}).get('provider', 'codex_subscription')
    if name != 'codex_subscription':
        raise ConfigurationError('Only verified ChatGPT subscription CLI access is enabled. '
                                 'API credit balances cannot currently be enforced, so API billing routes are disabled.')
    return CodexSubscription(db, settings)
