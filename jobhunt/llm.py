"""Gemini REST client (free tier) with RPM limiter + daily cap persisted in SQLite."""
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone

import requests

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(Exception):
    pass


class QuotaExceeded(LLMError):
    pass


class ModelUnavailable(LLMError):
    pass


class ConfigurationError(LLMError):
    pass


class TemporaryUnavailable(LLMError):
    pass


def parse_json(text: str):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", t, re.S)
        if m:
            return json.loads(m.group(1))
        raise LLMError(f"Non-JSON model output: {t[:200]}")


class Gemini:
    """Small REST client with an explicit free-model allowlist and durable cooldown."""

    def __init__(self, db, settings: dict, ignore_cooldown: bool = False):
        s = settings.get("llm", {})
        self.db = db
        override = os.environ.get("LLM_MODEL")
        configured = s.get("models") or [s.get("model", "gemini-3.8-flash"), s.get("fallback_model")]
        allowed = list(dict.fromkeys(m for m in configured if m))
        if not allowed:
            raise ConfigurationError("Set at least one free-tier model under llm.models")
        if override and override not in allowed:
            raise ConfigurationError(f"LLM_MODEL {override} is outside the configured free-tier model list")
        self.models = [override] if override else allowed
        last_success = db.one("SELECT model FROM llm_attempts WHERE outcome='ok' ORDER BY id DESC LIMIT 1")
        preferred = db.kv_get("llm_preferred_model") or (last_success["model"] if last_success else None)
        self.model = preferred if not override and preferred in self.models else self.models[0]
        self.rpm = max(1, int(s.get("rpm", 8)))
        self.cap = int(s.get("daily_cap", 150))
        self.attempts = max(1, int(s.get("attempts_per_model", 2)))
        self.timeout = (int(s.get("connect_timeout_sec", 5)), int(s.get("read_timeout_sec", 45)))
        self.cooldown_minutes = int(s.get("cooldown_minutes", 30))
        self.ignore_cooldown = ignore_cooldown
        self.key = os.environ.get("GEMINI_API_KEY", "")
        self._last = 0.0

    def cooldown_until(self):
        raw = self.db.kv_get("llm_cooldown_until")
        try:
            return datetime.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    def _gate(self):
        until = self.cooldown_until()
        if not self.ignore_cooldown and until and datetime.now(timezone.utc) < until:
            raise TemporaryUnavailable(f"Gemini cooling down until {until.isoformat()}")
        if self.db.llm_calls_today() >= self.cap:
            raise QuotaExceeded(f"Local daily cap {self.cap} reached")
        wait = 60.0 / self.rpm - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _record(self, model, outcome, started, http_status=None):
        self.db.record_llm_attempt(model, outcome, (time.monotonic() - started) * 1000, http_status)

    def _retry(self, model, attempt, reason, retry_after=None):
        delay = retry_after if retry_after is not None else 2 ** attempt + random.uniform(0, 1)
        delay = min(30.0, max(0.0, delay))
        print(f"[gemini] {model}: {reason}; retrying in {delay:.1f}s ({attempt + 2}/{self.attempts})", flush=True)
        time.sleep(delay)

    def _call(self, system: str, prompt: str, as_json: bool, retries: int | None = None) -> str:
        if not self.key:
            raise ConfigurationError("GEMINI_API_KEY not set")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"} if as_json else {},
        }
        attempts = self.attempts if retries is None else max(1, retries)
        models = [self.model] + [m for m in self.models if m != self.model]
        failures = []
        for model in models:
            for attempt in range(attempts):
                self._gate()
                self.db.bump_llm()  # Count attempts before sending, including timeouts.
                started = time.monotonic()
                try:
                    r = requests.post(API.format(model=model), json=body, timeout=self.timeout,
                                      headers={"x-goog-api-key": self.key, "Content-Type": "application/json"})
                except requests.RequestException as e:
                    kind = "timeout" if isinstance(e, requests.Timeout) else "network_error"
                    self._record(model, kind, started)
                    failures.append(f"{model}: {kind}")
                    if attempt + 1 < attempts:
                        self._retry(model, attempt, kind)
                    else:
                        print(f"[gemini] {model}: {kind}; attempts exhausted", flush=True)
                    continue
                code = r.status_code
                if code == 200:
                    try:
                        result = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    except (ValueError, KeyError, IndexError, TypeError) as e:
                        self._record(model, "bad_response", started, code)
                        raise LLMError(f"Gemini {model} returned an empty or malformed response") from e
                    self._record(model, "ok", started, code)
                    self.model = model
                    if not os.environ.get("LLM_MODEL"):
                        self.db.kv_set("llm_preferred_model", model)
                    self.db.kv_set("llm_cooldown_until", "")
                    return result
                if code == 404:
                    self._record(model, "model_unavailable", started, code)
                    failures.append(f"{model}: HTTP 404")
                    print(f"[gemini] {model} unavailable (404); trying next configured model", flush=True)
                    break
                if code in (400, 401, 402, 403):
                    self._record(model, "configuration_error", started, code)
                    raise ConfigurationError(f"Gemini HTTP {code}; check API key, project access, and request settings")
                if code in (408, 429, 500, 502, 503, 504):
                    kind = "rate_limited" if code == 429 else "service_unavailable"
                    self._record(model, kind, started, code)
                    failures.append(f"{model}: HTTP {code}")
                    if attempt + 1 < attempts:
                        retry_after = None
                        if code == 429:
                            try:
                                retry_after = float(r.headers.get("Retry-After", ""))
                            except (ValueError, AttributeError):
                                pass
                        self._retry(model, attempt, f"HTTP {code}", retry_after)
                    else:
                        print(f"[gemini] {model}: HTTP {code}; attempts exhausted", flush=True)
                    continue
                self._record(model, "http_error", started, code)
                raise LLMError(f"Gemini {model} returned HTTP {code}")
            if model != models[-1]:
                print(f"[gemini] switching to {models[models.index(model) + 1]}", flush=True)
        until = datetime.now(timezone.utc) + timedelta(minutes=self.cooldown_minutes)
        self.db.kv_set("llm_cooldown_until", until.isoformat(timespec="seconds"))
        summary = ", ".join(dict.fromkeys(failures))
        if failures and all("HTTP 404" in f for f in failures):
            raise ModelUnavailable("All configured Gemini models are unavailable (404); run `jobhunt llm-doctor`")
        if failures and all("HTTP 429" in f for f in failures):
            raise QuotaExceeded(f"Gemini rate/quota limit across configured models: {summary}; retry after {until.isoformat(timespec='seconds')}")
        raise TemporaryUnavailable(f"Gemini unavailable after configured retries ({summary}); retry after {until.isoformat(timespec='seconds')}")

    def json(self, system: str, prompt: str):
        return parse_json(self._call(system, prompt, True))

    def text(self, system: str, prompt: str) -> str:
        return self._call(system, prompt, False).strip()


class FakeLLM:
    """Test/dry-run stand-in. handler(system, prompt, as_json) -> str|dict"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = 0

    def json(self, system, prompt):
        self.calls += 1
        out = self.handler(system, prompt, True)
        return out if isinstance(out, (dict, list)) else parse_json(out)

    def text(self, system, prompt):
        self.calls += 1
        return str(self.handler(system, prompt, False))
