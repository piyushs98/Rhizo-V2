"""
LLM failover chain. Gemini first, DeepSeek second, silence third.

Scope, stated once and enforced everywhere: this produces COMMENTARY. It is
written to `scan_results.commentary` and rendered on the dashboard for a
human to read. It is never parsed, never keyword-matched, and never
influences a score, a size, or an exit.

Post-mortems #4 and #7 in v1 both came from model text leaking into control
flow - a substring match that suppressed scoring, and free-form decision tags
that broke a regex parser. The fix is not a stricter parser. The fix is that
no decision depends on this module at all. If both providers are down, the
desk trades exactly as it otherwise would, with an empty comment field.
"""
from __future__ import annotations

import logging

import requests

from app.config import settings
from app.resilience.timeouts import CallTimeout, budget

log = logging.getLogger("llm")


class LLMUnavailable(RuntimeError):
    pass


def _gemini(prompt: str, system: str) -> str:
    if not settings.gemini_api_key:
        raise LLMUnavailable("no Gemini key")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    r = requests.post(
        url,
        headers={"x-goog-api-key": settings.gemini_api_key,
                 "Content-Type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 400},
        },
        timeout=settings.llm_call_budget_s,
    )
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def _deepseek(prompt: str, system: str) -> str:
    if not settings.deepseek_api_key:
        raise LLMUnavailable("no DeepSeek key")
    r = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": settings.deepseek_model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 400,
        },
        timeout=settings.llm_call_budget_s,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


PROVIDERS = (("gemini", _gemini), ("deepseek", _deepseek))


def comment(prompt: str, system: str = "", *, default: str = "") -> str:
    """
    Best-effort commentary. Returns `default` on any failure.
    Never raises. Callers do not need a try block.
    """
    if not settings.llm_available:
        return default

    system = system or (
        "You are a trading desk analyst. Two or three sentences, plain "
        "language, no preamble. Describe what the numbers suggest and what "
        "would invalidate the read. Do not give an instruction to buy or sell."
    )

    for name, fn in PROVIDERS:
        try:
            out = budget(f"llm.{name}", settings.llm_call_budget_s,
                         fn, prompt, system)
            if out:
                return out
        except LLMUnavailable:
            continue
        except CallTimeout as exc:
            log.warning("[LLM FAILOVER] %s timed out: %s", name, exc)
        except Exception as exc:
            log.warning("[LLM FAILOVER] %s failed: %s", name, exc)

    log.warning("[LLM] every provider failed; continuing without commentary")
    return default
