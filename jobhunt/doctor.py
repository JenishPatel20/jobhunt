"""Small Gemini connectivity check that never sends profile or job data."""
import socket
import time

import requests

from .llm import Gemini, LLMError


def run(ctx) -> bool:
    host = "generativelanguage.googleapis.com"
    try:
        socket.getaddrinfo(host, 443)
        print(f"DNS: {host} reachable")
    except OSError as exc:
        print(f"DNS: failed ({type(exc).__name__}: {exc})")
        return False

    llm = Gemini(ctx.db, ctx.settings, ignore_cooldown=True)
    if not llm.key:
        print("API key: missing GEMINI_API_KEY")
        return False
    available = []
    for model in llm.models:
        started = time.monotonic()
        try:
            response = requests.get(f"https://{host}/v1beta/models/{model}",
                                    headers={"x-goog-api-key": llm.key}, timeout=llm.timeout)
            elapsed = round(time.monotonic() - started, 1)
            if response.status_code == 200:
                methods = response.json().get("supportedGenerationMethods", [])
                print(f"{model}: HTTP 200, generateContent={'yes' if 'generateContent' in methods else 'no'}, {elapsed}s")
                if "generateContent" in methods:
                    available.append(model)
            else:
                print(f"{model}: HTTP {response.status_code}, {elapsed}s")
        except requests.RequestException as exc:
            print(f"{model}: {type(exc).__name__} after {round(time.monotonic()-started, 1)}s")
    if not available:
        print("No configured model supports generation for this key. Check free-tier model access in AI Studio.")
        return False
    llm.model = available[0]
    llm.models = available
    try:
        answer = llm._call("Return only the word OK.", "Health check: reply OK.", False, retries=1)
    except LLMError as exc:
        print(f"Generation: {exc}")
        return False
    print(f"Generation: success with {llm.model} ({len(answer)} characters)")
    return True
