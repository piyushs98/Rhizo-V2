"""
BTC multi-layer scalping: VWAP, ATR sizing, layer precedence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.data.providers import Bar
from app.domain.models import (
    Direction, ExitPlan, ExitReason, Market, Position, Status,
)
from app.engine import scalping
from app.engine.exit_rules import evaluate

NOW = datetime(2026, 7, 24, 15, 0, tzinfo=timezone.utc)


def _bars(n=60, start=100.0, drift=0.002, vol=1.0):
    """Synthetic hour bars with mild uptrend above a rising VWAP."""
    out, price = [], start
    base = datetime(2026, 7, 20, tzinfo=timezone.utc)
    for i in range(n):
        price *= (1 + drift)
        out.append(Bar(
            ts=base + timedelta(hours=i),
            open=price * 0.999,
            high=price * 1.004,
            low=price * 0.996,
            close=price,
            volume=1000 * vol + (i % 3) * 50,
        ))
    return out


def _down_bars(n=60, start=100.0):
    return _bars(n=n, start=start, drift=-0.003)


# ------------------------------------------------------------------- VWAP
def test_vwap_uses_default_period_not_full_history():
    bars = _bars(80, start=50.0, drift=0.01)
    fullish = scalping.vwap(bars, period=80)
    default = scalping.vwap(bars)  # DEFAULT_VWAP_PERIOD
    short = scalping.vwap(bars, period=scalping.DEFAULT_VWAP_PERIOD)
    assert default == pytest.approx(short)
    # With strong drift, full history VWAP sits below the recent window.
    assert fullish != pytest.approx(default)


def test_vwap_and_floor_agree():
    bars = _bars(50)
    assert scalping.vwap(bars, 48) == pytest.approx(scalping.vwap_floor(bars, 48))


def test_vwap_empty_bars():
    assert scalping.vwap([]) is None


def test_vwap_zero_volume_falls_back():
    bars = _bars(20)
    for b in bars:
        b.volume = 0.0
    v = scalping.vwap(bars, 10)
    assert v is not None and v > 0


# ------------------------------------------------------------- entry gate
def test_entry_gate_passes_on_uptrend_above_vwap():
    bars = _bars(60, drift=0.003)
    ok, diag = scalping.entry_gate(bars, period=48, momentum_bars=6)
    assert ok is True
    assert diag["above_vwap"] == 1.0
    assert diag["momentum_ok"] == 1.0


def test_entry_gate_fails_on_downtrend():
    bars = _down_bars(60)
    ok, diag = scalping.entry_gate(bars, period=48, momentum_bars=6)
    assert ok is False


def test_entry_gate_fails_with_short_history():
    ok, _ = scalping.entry_gate(_bars(10), period=48, momentum_bars=6)
    assert ok is False


def test_entry_gate_fails_below_vwap():
    # Flat then dump the last close under VWAP.
    bars = _bars(50, drift=0.0)
    last = bars[-1]
    bars[-1] = Bar(
        ts=last.ts, open=last.open, high=last.high,
        low=1.0, close=1.0, volume=last.volume,
    )
    ok, diag = scalping.entry_gate(bars, period=48, momentum_bars=6)
    assert ok is False
    assert diag.get("above_vwap") == 0.0


# ----------------------------------------------------------- ATR plan math
def test_plan_stop_defines_r():
    bars = _bars(60)
    entry = bars[-1].close
    plan = scalping.build_plan(entry, bars, atr_mult=1.5, target_r=1.8,
                               trail_arm_r=0.8, max_hold_min=90, now=NOW)
    assert plan is not None
    assert plan.scalp is True
    assert plan.r_unit is not None and plan.r_unit > 0
    assert plan.stop_price == pytest.approx(entry - plan.r_unit, rel=1e-6)
    assert plan.target_price == pytest.approx(entry + 1.8 * plan.r_unit, rel=1e-6)
    assert plan.trail_activate_at == pytest.approx(entry + 0.8 * plan.r_unit, rel=1e-6)
    assert plan.time_stop_ts == NOW + timedelta(minutes=90)
    assert plan.vwap_floor is not None


def test_plan_levels_are_on_btc_scale_not_equity_pct():
    """
    Regression: equity-style 35%/60% on a 68k BTC put stop ~44k and target
    ~108k. ATR plan must stay within a few percent of entry.
    """
    bars = _bars(60, start=68000.0, drift=0.0005)
    entry = bars[-1].close
    plan = scalping.build_plan(entry, bars, atr_mult=1.5, now=NOW)
    assert plan is not None
    assert plan.stop_price > entry * 0.90
    assert plan.target_price < entry * 1.10


def test_plan_rejects_bad_entry():
    assert scalping.build_plan(0.0, _bars(40)) is None
    assert scalping.build_plan(100.0, []) is None


def test_r_multiple_helper():
    assert scalping.r_multiple(100, 103, 2.0) == pytest.approx(1.5)
    assert scalping.r_multiple(100, 100, 0.0) is None


# --------------------------------------------------- exit layer precedence
def _scalp_pos(entry=100.0, stop=97.0, target=105.4, vwap_floor=99.0,
               hw=None, giveback=0.35, time_stop=None):
    r = entry - stop
    return Position(
        position_id="s1", idempotency_key="k", market=Market.CRYPTO_SPOT,
        underlying="BTC-USD", instrument="BTC-USD",
        direction=Direction.LONG_SPOT, status=Status.OPEN,
        quantity=0.1, multiplier=1.0, entry_price=entry,
        plan=ExitPlan(
            stop_price=stop, target_price=target,
            trail_activate_at=entry + 0.8 * r,
            trail_giveback_pct=giveback, trail_high_water=hw,
            time_stop_ts=time_stop, scalp=True, r_unit=r,
            vwap_floor=vwap_floor,
        ),
    )


def test_stop_loss_still_first_on_scalp():
    p = _scalp_pos(stop=98.0, vwap_floor=99.0)
    s = evaluate(p, 97.5, now=NOW)
    assert s.reason is ExitReason.STOP_LOSS


def test_vwap_break_second_after_stop():
    p = _scalp_pos(stop=90.0, vwap_floor=99.0)
    s = evaluate(p, 98.5, now=NOW)
    assert s.should_close and s.reason is ExitReason.VWAP_BREAK


def test_vwap_break_does_not_fire_when_above_floor():
    p = _scalp_pos(stop=90.0, vwap_floor=99.0)
    assert evaluate(p, 99.5, now=NOW).should_close is False


def test_non_scalp_ignores_vwap_floor():
    p = _scalp_pos(stop=90.0, vwap_floor=99.0)
    p.plan.scalp = False
    assert evaluate(p, 98.5, now=NOW).should_close is False


def test_trailing_on_scalp():
    p = _scalp_pos(stop=90.0, vwap_floor=50.0, hw=110.0, giveback=0.10)
    # trail at 99.0
    assert evaluate(p, 98.5, now=NOW, lock_pct=0.0).reason is ExitReason.TRAILING_STOP


def test_take_profit_on_scalp():
    p = _scalp_pos(stop=90.0, vwap_floor=50.0, target=105.0)
    assert evaluate(p, 105.0, now=NOW, lock_pct=0.0).reason is ExitReason.TAKE_PROFIT


def test_time_stop_on_scalp():
    p = _scalp_pos(stop=90.0, vwap_floor=50.0,
                   time_stop=NOW - timedelta(minutes=1))
    assert evaluate(p, 100.0, now=NOW, lock_pct=0.0).reason is ExitReason.TIME_STOP


def test_stop_beats_vwap_when_both_hit():
    p = _scalp_pos(stop=99.0, vwap_floor=99.5)
    assert evaluate(p, 98.0, now=NOW, lock_pct=0.0).reason is ExitReason.STOP_LOSS


def test_vwap_beats_trailing_when_both_hit():
    p = _scalp_pos(stop=80.0, vwap_floor=100.0, hw=120.0, giveback=0.10)
    # mark 99: below vwap 100 and below trail 108
    assert evaluate(p, 99.0, now=NOW, lock_pct=0.0).reason is ExitReason.VWAP_BREAK


def test_position_r_multiple():
    p = _scalp_pos(entry=100.0, stop=98.0)
    p.mark_price = 101.0
    assert p.r_multiple() == pytest.approx(0.5)


def test_default_vwap_period_constant():
    assert scalping.DEFAULT_VWAP_PERIOD == 48
