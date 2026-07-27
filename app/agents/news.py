"""
Rolling News / Sentiment Agent.

The only path where a language model influences a trade decision — and even
here, the only thing that leaves this module is a float in [-1, 1].

Refreshes on an interval during EQUITY (MACRO scope) and CRYPTO (CRYPTO
scope), not only at PREP. Separate scopes so a risk-off equity print cannot
tilt the overnight BTC book.

Contract:
  - call the LLM with a strict-JSON instruction
  - parse `{"bias": <number>}` (optionally with a short note)
  - range-check to [-1, 1]
  - append the REAL and return it

Every failure mode resolves to 0.0 (neutral). Never raises.
Scoring imports neither this module nor the LLM chain.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from app.config import settings
from app.db import repositories as repo
from app.llm import chain as llm

log = logging.getLogger("agents.news")

Scope = Literal["MACRO", "CRYPTO"]
SCOPES: tuple[Scope, ...] = ("MACRO", "CRYPTO")

_SYSTEM = {
    "MACRO": (
        "You are an equity desk analyst. Reply with ONLY a JSON object, "
        "no markdown fences, no prose outside the object. Schema: "
        '{"bias": <float between -1.0 and 1.0>, "note": "<one short sentence>"}. '
        "bias is the directional tilt of live US large-cap tech and major "
        "indices: -1 strongly bearish, 0 neutral, +1 strongly bullish. "
        "Prefer near-zero when evidence is mixed."
    ),
    "CRYPTO": (
        "You are a crypto desk analyst. Reply with ONLY a JSON object, "
        "no markdown fences, no prose outside the object. Schema: "
        '{"bias": <float between -1.0 and 1.0>, "note": "<one short sentence>"}. '
        "bias is the directional tilt for major crypto (BTC, ETH majors): "
        "-1 strongly bearish, 0 neutral, +1 strongly bullish. Prefer "
        "near-zero when evidence is mixed. Do not mirror equity risk-off "
        "unless crypto-specific evidence supports it."
    ),
}

_USER = {
    "MACRO": (
        "Summarise the current US equity tape bias. "
        "Universe: NVDA, AAPL, MSFT, AMZN, GOOGL, META, TSLA, SPY, QQQ, IWM. "
        "Return the JSON object now."
    ),
    "CRYPTO": (
        "Summarise the current crypto tape bias for BTC and major alts. "
        "Return the JSON object now."
    ),
}


def _clamp_bias(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text or not isinstance(text, str):
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
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
    """Validate model text into a float in [-1, 1], or None if unusable."""
    obj = _extract_json(raw)
    if obj is None:
        return None
    if "bias" not in obj:
        return None
    raw_bias = obj["bias"]
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


def _normalize_scope(scope: str) -> Scope:
    s = (scope or "MACRO").upper()
    return "CRYPTO" if s == "CRYPTO" else "MACRO"


def refresh(
    scope: str = "MACRO",
    *,
    session_date: str | None = None,
    force: bool = False,
) -> float:
    """
    Rolling refresh for one scope. Always appends a row. Never raises.

    force=True skips the "already fresh enough" short-circuit (used by the
    dashboard REFRESH_NEWS command).
    """
    try:
        return _refresh_impl(scope, session_date=session_date, force=force)
    except Exception as exc:
        log.exception("news refresh failed closed: %s", exc)
        return 0.0


def _refresh_impl(
    scope: str,
    *,
    session_date: str | None,
    force: bool,
) -> float:
    sc = _normalize_scope(scope)
    if not settings.news_enabled:
        return 0.0

    day = session_date or datetime.now(tz=timezone.utc).date().isoformat()

    if not force:
        age = bias_age_seconds(sc)
        if age is not None and age < settings.news_refresh_interval_s:
            return current_bias(sc)

    if not settings.llm_available:
        log.info("news agent [%s]: no LLM; recording neutral", sc)
        repo.sentiment.store(
            session_date=day, bias=0.0, source="none",
            note="llm unavailable", scope=sc,
        )
        return 0.0

    raw = llm.comment(_USER[sc], system=_SYSTEM[sc], default="")
    bias = parse_bias(raw)
    if bias is None:
        log.warning("news agent [%s]: unusable output; fail closed", sc)
        repo.sentiment.store(
            session_date=day, bias=0.0, source="parse_fail",
            raw_json=raw[:2000] if raw else "",
            note="malformed or out-of-range response", scope=sc,
        )
        return 0.0

    note = ""
    obj = _extract_json(raw) or {}
    if isinstance(obj.get("note"), str):
        note = obj["note"][:200]

    repo.sentiment.store(
        session_date=day,
        bias=bias,
        source="llm",
        raw_json=raw[:2000],
        note=note,
        scope=sc,
    )
    log.info("news agent [%s]: bias=%.3f", sc, bias)
    return bias


def run(*, session_date: str, force: bool = False) -> float:
    """Back-compat entry used by older call sites. MACRO scope."""
    return refresh("MACRO", session_date=session_date, force=force)


def current_bias(scope: str = "MACRO") -> float:
    """What scoring should read. 0.0 when expired or missing."""
    sc = _normalize_scope(scope)
    return repo.sentiment.latest_bias(
        max_age_hours=settings.news_bias_ttl_hours, scope=sc
    )


def bias_age_seconds(scope: str = "MACRO") -> float | None:
    """Age of the latest row for scope, or None if missing."""
    sc = _normalize_scope(scope)
    row = repo.sentiment.latest(scope=sc)
    if not row:
        return None
    created = row.get("created_at")
    if not created:
        return None
    try:
        ts = datetime.fromisoformat(str(created))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(tz=timezone.utc) - ts).total_seconds())


def status() -> dict[str, Any]:
    """Both scopes with ages — for the dashboard and doctor."""
    out: dict[str, Any] = {}
    for sc in SCOPES:
        row = repo.sentiment.latest(
            max_age_hours=settings.news_bias_ttl_hours, scope=sc
        )
        age = bias_age_seconds(sc)
        out[sc.lower()] = {
            "scope": sc,
            "bias": float(row["bias"]) if row else 0.0,
            "fresh": row is not None,
            "age_seconds": round(age, 1) if age is not None else None,
            "stale": (
                age is None
                or age > settings.news_bias_ttl_hours * 3600.0
            ),
            "session_date": row.get("session_date") if row else None,
            "created_at": row.get("created_at") if row else None,
            "note": (row.get("note") or "") if row else "",
            "source": (row.get("source") or "") if row else "",
        }
    return out
