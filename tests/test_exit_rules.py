"""Exit rules. Precedence and each individual trigger."""
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.models import (
    Direction, ExitPlan, ExitReason, Market, Position, Status,
)
from app.engine.exit_rules import evaluate

NOW = datetime(2026, 7, 24, 15, 0, tzinfo=timezone.utc)


def pos(entry=5.00, stop=3.25, target=8.00, hw=None, giveback=0.15,
        time_stop=None, market=Market.EQUITY_OPTION):
    return Position(
        position_id="p1", idempotency_key="k1", market=market,
        underlying="NVDA", instrument="NVDA260130C00150000",
        direction=Direction.LONG_CALL, status=Status.OPEN,
        quantity=1, multiplier=100, entry_price=entry,
        plan=ExitPlan(stop_price=stop, target_price=target,
                      trail_activate_at=entry * 1.30,
                      trail_giveback_pct=giveback, trail_high_water=hw,
                      time_stop_ts=time_stop),
    )


def _ev(p, mark, **kw):
    # Default lock-in off in legacy tests so trail/stop cases stay isolated.
    kw.setdefault("lock_pct", 0.0)
    return evaluate(p, mark, now=NOW, **kw)


def test_hold_in_the_middle():
    assert _ev(pos(), 5.50).should_close is False


def test_stop_loss_fires_at_the_level():
    s = _ev(pos(), 3.25)
    assert s.should_close and s.reason is ExitReason.STOP_LOSS


def test_stop_loss_fires_below_the_level():
    assert _ev(pos(), 2.10).reason is ExitReason.STOP_LOSS


def test_take_profit_fires_at_the_target():
    s = _ev(pos(), 8.00)
    assert s.should_close and s.reason is ExitReason.TAKE_PROFIT


def test_trailing_stop_fires_after_giving_back():
    # high water 7.00, 15% giveback => trail at 5.95
    assert _ev(pos(hw=7.00), 5.90).reason is ExitReason.TRAILING_STOP
    assert _ev(pos(hw=7.00), 6.10).should_close is False


def test_trailing_stop_is_inactive_before_it_arms():
    assert _ev(pos(hw=None), 5.90).should_close is False


def test_time_stop():
    expired = pos(time_stop=NOW - timedelta(minutes=1))
    s = _ev(expired, 5.50)
    assert s.should_close and s.reason is ExitReason.TIME_STOP


def test_session_flatten_applies_to_options_only():
    assert _ev(pos(), 5.50, session_flatten=True).reason is ExitReason.SESSION_FLATTEN
    crypto = pos(market=Market.CRYPTO_SPOT)
    assert _ev(crypto, 5.50, session_flatten=True).should_close is False


# ------------------------------------------------------------- precedence
def test_stop_loss_beats_everything():
    """Capital preservation first, even if the target is somehow also hit."""
    p = pos(stop=6.00, target=6.00, hw=9.00, time_stop=NOW - timedelta(hours=1))
    assert _ev(p, 6.00).reason is ExitReason.STOP_LOSS


def test_trailing_beats_take_profit():
    p = pos(target=5.00, hw=10.00)   # trail at 8.50
    assert _ev(p, 8.40).reason is ExitReason.TRAILING_STOP


def test_take_profit_beats_time_stop():
    p = pos(time_stop=NOW - timedelta(hours=2))
    assert _ev(p, 8.50).reason is ExitReason.TAKE_PROFIT


# ------------------------------------------------------------ safety rails
def test_no_plan_means_hold():
    p = pos()
    p.plan = None
    assert _ev(p, 1.00).should_close is False


def test_zero_mark_never_triggers_a_stop():
    """A bad price print must not be mistaken for a wipeout."""
    assert _ev(pos(), 0.0).should_close is False


def test_lock_in_raises_effective_stop_to_plus_20():
    """Once mark is +20%, that price is the stop floor."""
    from app.engine.exit_rules import effective_stop

    p = pos(entry=10.0, stop=6.5, target=16.0)
    # Not yet at +20%
    assert effective_stop(p, 11.0, lock_pct=0.20) == 6.5
    # At +20% → stop floor 12.0
    assert effective_stop(p, 12.0, lock_pct=0.20) == pytest.approx(12.0)
    # After arming, pullback to 12.0 exits as stop
    s = evaluate(p, 12.0, now=NOW, lock_pct=0.20)
    # mark == lock-in stop → close
    assert s.should_close and s.reason is ExitReason.STOP_LOSS
    assert "lock-in" in s.detail


def test_lock_in_holds_above_floor_after_arm():
    p = pos(entry=10.0, stop=6.5, target=16.0, hw=13.0)  # has printed above +20%
    s = evaluate(p, 12.5, now=NOW, lock_pct=0.20)
    assert s.should_close is False
    # Below lock-in 12.0 after arming via high water
    s2 = evaluate(p, 11.9, now=NOW, lock_pct=0.20)
    assert s2.should_close and s2.reason is ExitReason.STOP_LOSS


def test_closed_position_is_left_alone():
    p = pos()
    p.status = Status.CLOSED
    assert evaluate(p, 1.00, now=NOW).should_close is False


def test_stale_mark_is_surfaced_but_does_not_close():
    s = evaluate(pos(), 5.50, now=NOW, mark_age_s=3600)
    assert s.should_close is False
    assert "old" in s.detail


# --------------------------------------------------------------- plan math
def test_plan_builds_symmetric_levels():
    plan = ExitPlan.build(10.0, stop_pct=0.35, target_pct=0.60,
                          trail_activate_pct=0.30, trail_giveback_pct=0.15,
                          max_hold_hours=48, now=NOW)
    assert plan.stop_price == pytest.approx(6.50)
    assert plan.target_price == pytest.approx(16.00)
    assert plan.trail_activate_at == pytest.approx(13.00)
    assert plan.time_stop_ts == NOW + timedelta(hours=48)
