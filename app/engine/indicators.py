"""
Technical indicators. Pure functions over lists of floats.

No pandas, no I/O, no globals. Every one of these is directly unit-testable,
which is the point: post-mortem #4 was a scoring defect that ran undetected
for an unknown period because nothing in v1 could be tested in isolation.
"""
from __future__ import annotations

from statistics import fmean, pstdev

from app.data.providers import Bar


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return fmean(values[-period:])


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    k = 2 / (period + 1)
    out = fmean(values[:period])
    for v in values[period:]:
        out = v * k + out * (1 - k)
    return out


def true_range(bars: list[Bar]) -> list[float]:
    if len(bars) < 2:
        return []
    out = []
    for prev, cur in zip(bars, bars[1:]):
        out.append(max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        ))
    return out


def atr(bars: list[Bar], period: int = 14) -> float | None:
    tr = true_range(bars)
    if len(tr) < period:
        return None
    return fmean(tr[-period:])


def atr_pct(bars: list[Bar], period: int = 14) -> float | None:
    a = atr(bars, period)
    if a is None or not bars or bars[-1].close == 0:
        return None
    return a / bars[-1].close


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(values[-(period + 1):], values[-period:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain, avg_loss = fmean(gains), fmean(losses)
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def momentum_pct(values: list[float], lookback: int = 10) -> float | None:
    if len(values) < lookback + 1 or values[-(lookback + 1)] == 0:
        return None
    return (values[-1] / values[-(lookback + 1)] - 1) * 100.0


def realized_vol(values: list[float], period: int = 20) -> float | None:
    if len(values) < period + 1:
        return None
    rets = [
        (b / a - 1) for a, b in zip(values[-(period + 1):], values[-period:]) if a
    ]
    if len(rets) < 2:
        return None
    return pstdev(rets)


def volume_ratio(bars: list[Bar], period: int = 20) -> float | None:
    """Latest volume against its own average. >1 means unusual activity."""
    if len(bars) < period + 1:
        return None
    avg = fmean(b.volume for b in bars[-(period + 1):-1])
    if avg <= 0:
        return None
    return bars[-1].volume / avg


def range_position(bars: list[Bar], period: int = 20) -> float | None:
    """Where the last close sits in the recent range, 0..1."""
    if len(bars) < period:
        return None
    window = bars[-period:]
    hi = max(b.high for b in window)
    lo = min(b.low for b in window)
    if hi <= lo:
        return None
    return (bars[-1].close - lo) / (hi - lo)


def classic_pivots(prev: Bar) -> dict[str, float]:
    p = (prev.high + prev.low + prev.close) / 3
    return {
        "pivot": p,
        "r1": 2 * p - prev.low,
        "s1": 2 * p - prev.high,
        "r2": p + (prev.high - prev.low),
        "s2": p - (prev.high - prev.low),
    }


def trend_score(closes: list[float]) -> float:
    """
    -1..+1. Combines the fast/slow moving-average relationship with where
    price sits against the slow average. Deliberately simple and legible;
    every input is visible on the dashboard.
    """
    fast, slow = ema(closes, 9), ema(closes, 21)
    if fast is None or slow is None or slow == 0:
        return 0.0
    spread = (fast - slow) / slow
    above = (closes[-1] - slow) / slow
    raw = (spread * 12) + (above * 6)
    return max(-1.0, min(1.0, raw))
