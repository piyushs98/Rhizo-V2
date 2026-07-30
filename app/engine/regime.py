"""
Market regime classification from SPY. Pure functions, no I/O, no LLM.

Risk-on  : SPY above its 20-day MA and short-term slope non-negative.
Risk-off : otherwise (below MA, or negative slope, or insufficient data).

Used as a named risk gate on the equity desk: block long-call / long-share
entries in risk-off; block long-put entries in risk-on.
"""
from __future__ import annotations

from enum import Enum

from app.engine.indicators import sma


class MarketRegime(str, Enum):
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    UNKNOWN = "unknown"


def classify_spy_regime(
    closes: list[float],
    *,
    ma_period: int = 20,
    slope_lookback: int = 5,
) -> MarketRegime:
    """
    Classify regime from SPY daily closes (oldest → newest).

    - Need at least `ma_period` closes; otherwise UNKNOWN (caller treats as
      fail-open or fail-closed — equity adapter fails open with a note).
    - risk_on only when last close > SMA(ma_period) AND slope over the last
      `slope_lookback` bars is >= 0.
    - Everything else is risk_off (including flat-to-down while above MA).
    """
    if ma_period <= 0 or slope_lookback <= 0:
        return MarketRegime.UNKNOWN
    if len(closes) < ma_period:
        return MarketRegime.UNKNOWN

    ma = sma(closes, ma_period)
    if ma is None or ma <= 0:
        return MarketRegime.UNKNOWN

    price = closes[-1]
    # Slope: change from close[-slope_lookback-1] to close[-1], need that bar.
    if len(closes) < slope_lookback + 1:
        return MarketRegime.UNKNOWN
    base = closes[-(slope_lookback + 1)]
    if base <= 0:
        return MarketRegime.UNKNOWN
    slope = price - base

    if price > ma and slope >= 0:
        return MarketRegime.RISK_ON
    return MarketRegime.RISK_OFF


def blocks_direction(regime: MarketRegime, direction_value: str) -> str | None:
    """
    Return a human reason if `direction` is blocked in this regime, else None.

    LONG_CALL / LONG_SHARE blocked in risk-off.
    LONG_PUT blocked in risk-on.
    UNKNOWN never blocks (insufficient data ≠ a call on risk).
    """
    d = (direction_value or "").upper()
    if regime is MarketRegime.RISK_OFF and d in {"LONG_CALL", "LONG_SHARE"}:
        return (
            "SPY regime is risk-off (below 20d MA or negative short-term slope); "
            f"{d} entries are blocked."
        )
    if regime is MarketRegime.RISK_ON and d == "LONG_PUT":
        return (
            "SPY regime is risk-on (above 20d MA with non-negative slope); "
            "LONG_PUT entries are blocked."
        )
    return None
