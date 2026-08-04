"""
EMA crossover signals. Pure functions for research and optional crypto entry.

Not wired as the live crypto strategy unless a fee-aware backtest shows
profit factor > 1 (see scripts/research_ma_crossover.py).
"""
from __future__ import annotations

from app.data.providers import Bar
from app.engine.indicators import ema


def ema_series(closes: list[float], period: int) -> list[float | None]:
    """EMA at each index; None until `period` samples exist."""
    if period <= 0 or len(closes) < period:
        return [None] * len(closes)
    k = 2.0 / (period + 1)
    out: list[float | None] = [None] * (period - 1)
    seed = sum(closes[:period]) / period
    out.append(seed)
    e = seed
    for v in closes[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def detect_crosses(
    closes: list[float],
    fast: int,
    slow: int,
) -> list[tuple[int, int]]:
    """
    Return list of (index, direction) where direction is +1 bullish cross
    (fast rises above slow) or -1 bearish cross.
    """
    if fast >= slow:
        raise ValueError("fast period must be < slow period")
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    out: list[tuple[int, int]] = []
    for i in range(1, len(closes)):
        if None in (ef[i], es[i], ef[i - 1], es[i - 1]):
            continue
        prev = ef[i - 1] - es[i - 1]  # type: ignore[operator]
        cur = ef[i] - es[i]  # type: ignore[operator]
        if prev <= 0 and cur > 0:
            out.append((i, 1))
        elif prev >= 0 and cur < 0:
            out.append((i, -1))
    return out


def last_cross_signal(
    bars: list[Bar],
    fast: int = 21,
    slow: int = 50,
    *,
    lookback: int = 3,
) -> tuple[int | None, dict]:
    """
    Most recent cross within the last `lookback` bars, or None.

    Returns (direction, diagnostics) with direction in {+1, -1, None}.
    """
    closes = [b.close for b in bars]
    crosses = detect_crosses(closes, fast, slow)
    diag: dict = {"fast": float(fast), "slow": float(slow)}
    if not crosses:
        return None, diag
    i, d = crosses[-1]
    diag["bars_since_cross"] = float(len(closes) - 1 - i)
    diag["direction"] = float(d)
    if len(closes) - 1 - i <= lookback:
        return d, diag
    return None, diag
