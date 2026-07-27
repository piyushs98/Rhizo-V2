"""
BTC multi-layer scalping.

Six layers, all volatility-derived rather than fixed percentages:

  1. Entry gate     price above VWAP + short-horizon momentum positive
  2. Stop           entry − ATR_MULT × ATR   (this distance is R)
  3. Target         entry + TARGET_R × R
  4. Trail arm      entry + TRAIL_ARM_R × R
  5. VWAP break     exit when mark falls through the live VWAP floor
  6. Time stop      minutes-scale, not hours

Why this exists: the equity-style percentage plan on BTC put the stop at
~44,200 and the target at ~108,800 for a 68k entry — crypto could only ever
have exited on the time stop. ATR-scaled R keeps the levels on the same
scale as the tape.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.data.providers import Bar
from app.domain.models import ExitPlan
from app.engine import indicators as ind

# Unified default so vwap(), entry_gate(), and vwap_floor() agree.
DEFAULT_VWAP_PERIOD = 48


def vwap(bars: list[Bar], period: int | None = None) -> float | None:
    """
    Volume-weighted average price over the last `period` bars.

    Defaults to DEFAULT_VWAP_PERIOD (not full history). That keeps this helper
    consistent with the entry gate and the live VWAP floor used on exits.
    """
    if not bars:
        return None
    n = period if period is not None else DEFAULT_VWAP_PERIOD
    if n <= 0:
        return None
    window = bars[-n:] if len(bars) >= n else bars
    total_vol = sum(b.volume for b in window)
    if total_vol <= 0:
        # Fall back to a plain average of typical price.
        tps = [((b.high + b.low + b.close) / 3.0) for b in window]
        return sum(tps) / len(tps) if tps else None
    num = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in window)
    return num / total_vol


def vwap_floor(bars: list[Bar], period: int | None = None) -> float | None:
    """Live floor used by the VWAP-break exit layer. Same window as entry."""
    p = period if period is not None else settings.scalp_vwap_period
    return vwap(bars, p)


def entry_gate(
    bars: list[Bar],
    *,
    period: int | None = None,
    momentum_bars: int | None = None,
) -> tuple[bool, dict[str, float]]:
    """
    VWAP + momentum entry gate.

    Passes when the last close is at or above VWAP and short-horizon
    momentum is non-negative. Returns (ok, diagnostics).
    """
    p = period if period is not None else settings.scalp_vwap_period
    m = momentum_bars if momentum_bars is not None else settings.scalp_momentum_bars
    diag: dict[str, float] = {}

    if len(bars) < max(p, m + 1, 20):
        return False, {"reason": 0.0}  # not enough history

    level = vwap(bars, p)
    if level is None or level <= 0:
        return False, diag

    close = bars[-1].close
    diag["vwap"] = round(level, 4)
    diag["close"] = round(close, 4)
    diag["above_vwap"] = 1.0 if close >= level else 0.0

    closes = [b.close for b in bars]
    mom = ind.momentum_pct(closes, m)
    if mom is None:
        return False, diag
    diag["momentum_pct"] = round(mom, 4)
    diag["momentum_ok"] = 1.0 if mom >= 0 else 0.0

    ok = close >= level and mom >= 0
    return ok, diag


def build_plan(
    entry_price: float,
    bars: list[Bar],
    *,
    atr_period: int = 14,
    now: datetime | None = None,
    atr_mult: float | None = None,
    target_r: float | None = None,
    trail_arm_r: float | None = None,
    trail_giveback_pct: float | None = None,
    max_hold_min: float | None = None,
    vwap_period: int | None = None,
) -> ExitPlan | None:
    """
    Build an ATR-scaled scalp ExitPlan, or None if inputs are unusable.
    """
    if entry_price <= 0 or not bars:
        return None

    atr_value = ind.atr(bars, atr_period)
    if atr_value is None or atr_value <= 0:
        return None

    mult = atr_mult if atr_mult is not None else settings.scalp_atr_mult
    t_r = target_r if target_r is not None else settings.scalp_target_r
    a_r = trail_arm_r if trail_arm_r is not None else settings.scalp_trail_arm_r
    giveback = (
        trail_giveback_pct if trail_giveback_pct is not None
        else settings.scalp_trail_giveback_pct
    )
    hold_min = max_hold_min if max_hold_min is not None else settings.scalp_max_hold_min
    vp = vwap_period if vwap_period is not None else settings.scalp_vwap_period

    r_unit = atr_value * mult
    if r_unit <= 0:
        return None

    floor = vwap_floor(bars, vp)
    now = now or datetime.now(tz=timezone.utc)

    return ExitPlan(
        stop_price=round(entry_price - r_unit, 6),
        target_price=round(entry_price + t_r * r_unit, 6),
        trail_activate_at=round(entry_price + a_r * r_unit, 6),
        trail_giveback_pct=giveback,
        trail_high_water=None,
        time_stop_ts=now + timedelta(minutes=hold_min),
        scalp=True,
        r_unit=round(r_unit, 6),
        vwap_floor=round(floor, 6) if floor is not None else None,
    )


def r_multiple(entry: float, mark: float, r_unit: float) -> float | None:
    if r_unit <= 0:
        return None
    return (mark - entry) / r_unit
