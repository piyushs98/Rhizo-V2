"""
PREP-shift News / Sentiment Agent.

The only path where a language model influences a trade decision — and even
here, the only thing that leaves this module is a float in [-1, 1].

Contract:
  - call the LLM with a strict-JSON instruction
  - parse `{"bias": <number>}` (optionally with a short note)
  - range-check to [-1, 1]
  - store the REAL and return it

Every failure mode resolves to 0.0 (neutral). Prose, malformed JSON, out-of-
range values, missing keys, provider outages, empty responses — all fail
closed. Scoring imports neither this module nor the LLM chain; it only
reads the float from SentimentRepo.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import settings
from app.db import repositories as repo
from app.llm import chain as llm

log = logging.getLogger("agents.news")

SYSTEM = (
    "You are a pre-market equity desk analyst. Reply with ONLY a JSON object, "
    "no markdown fences, no prose outside the object. Schema: "
    '{"bias": <float between -1.0 and 1.0>, "note": "<one short sentence>"}. '
    "bias is the directional tilt of overnight and pre-market news for US "
    "large-cap tech and major indices: -1 strongly bearish, 0 neutral, "
    "+1 strongly bullish. Prefer near-zero when evidence is mixed."
)

USER_PROMPT = (
    "Summarise the pre-market tape for the US equity session. "
    "Universe: NVDA, AAPL, MSFT, AMZN, GOOGL, META, TSLA, SPY, QQQ, IWM. "
    "Return the JSON object now."
)


def _clamp_bias(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _extract_json(text: str) -> dict[str, Any] | None:
    """
    Pull a JSON object out of model output. Accepts raw JSON or a fenced
    block. Returns None on any parse failure — never raises.
    """
    if not text or not isinstance(text, str):
        return None
    cleaned = text.strip()
    # Strip ```json ... ``` fences if the model ignored instructions.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    # If there is leading prose, take the first {...} span.
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        cleaned = cleaned[start : end + 1]
    try:
        obj = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def parse_bias(raw: str) -> float | None:
    """
    Validate model text into a float in [-1, 1], or None if unusable.

    Exposed for unit tests. The production path goes through run().
    """
    obj = _extract_json(raw)
    if obj is None:
        return None
    if "bias" not in obj:
        return None
    raw_bias = obj["bias"]
    # bool is a subclass of int; float(True) == 1.0 — reject explicitly.
    if isinstance(raw_bias, bool) or raw_bias is None:
        return None
    try:
        value = float(raw_bias)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    if abs(value) > 1.0 + 1e-9:
        return None
    return _clamp_bias(value)


def run(
    *,
    session_date: str,
    force: bool = False,
) -> float:
    """
    Execute the PREP news pass once per session.

    Returns the stored bias (or 0.0). Idempotent within a session unless
    `force=True`: a second call on the same session_date reuses the row.
    """
    if not settings.news_enabled:
        return 0.0

    if not force:
        existing = repo.sentiment.latest(max_age_hours=settings.news_bias_ttl_hours)
        if existing and existing.get("session_date") == session_date:
            try:
                return float(existing["bias"])
            except (TypeError, ValueError, KeyError):
                return 0.0

    if not settings.llm_available:
        log.info("news agent: no LLM available; recording neutral bias")
        repo.sentiment.store(
            session_date=session_date, bias=0.0, source="none",
            note="llm unavailable",
        )
        return 0.0

    raw = llm.comment(USER_PROMPT, system=SYSTEM, default="")
    bias = parse_bias(raw)
    if bias is None:
        log.warning("news agent: unusable model output; failing closed to 0.0")
        repo.sentiment.store(
            session_date=session_date, bias=0.0, source="parse_fail",
            raw_json=raw[:2000] if raw else "",
            note="malformed or out-of-range response",
        )
        return 0.0

    # Prefer the provider that answered, if chain left a hint; otherwise "llm".
    source = "llm"
    note = ""
    obj = _extract_json(raw) or {}
    if isinstance(obj.get("note"), str):
        note = obj["note"][:200]

    repo.sentiment.store(
        session_date=session_date,
        bias=bias,
        source=source,
        raw_json=raw[:2000],
        note=note,
    )
    log.info("news agent: session %s bias=%.3f", session_date, bias)
    return bias


def current_bias() -> float:
    """What scoring should read right now. 0.0 when expired or missing."""
    return repo.sentiment.latest_bias(max_age_hours=settings.news_bias_ttl_hours)
