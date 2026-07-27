"""
Position store.

The first test in this file is the most important test in the project: it is
the one that proves the duplicate-trade bug cannot recur.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.db import repositories as repo
from app.db.repositories import TransitionError
from app.domain.models import (
    Direction, ExitPlan, Market, OrderIntent, Position, Status,
)


def make_intent(symbol="NVDA", session="EQ-2026-07-24", price=5.00, scan="s1"):
    return OrderIntent(
        market=Market.EQUITY_OPTION,
        underlying=symbol,
        instrument=f"{symbol}260130C00150000",
        direction=Direction.LONG_CALL,
        quantity=2,
        multiplier=100,
        limit_price=price,
        session_key=session,
        scan_id=scan,
        score=78.5,
        plan=ExitPlan.build(price, stop_pct=0.35, target_pct=0.60,
                            trail_activate_pct=0.30, trail_giveback_pct=0.15,
                            max_hold_hours=48),
    )


# ======================================================================
# THE regression test
# ======================================================================
def test_rescanning_the_same_name_does_not_open_a_second_position():
    """
    v1's central defect: every scan that scored a symbol above the threshold
    called paper_buy again, stacking a fresh contract onto a name already
    held. Ten scans in a session meant ten NVDA positions.

    Here the second, third and tenth attempts are no-ops that return the
    original position.
    """
    first, created = repo.positions.open_position(make_intent(scan="scan-1"), 5.10)
    assert created is True

    for n in range(2, 11):
        pos, created = repo.positions.open_position(
            make_intent(scan=f"scan-{n}"), 5.20 + n
        )
        assert created is False, f"scan {n} opened a duplicate"
        assert pos.position_id == first.position_id
        assert pos.entry_price == 5.10, "the original fill must not be overwritten"

    assert repo.positions.open_count() == 1
    assert repo.positions.open_count_for("NVDA") == 1


def test_a_new_session_permits_a_new_position():
    """The lock is per shift, not forever. Tomorrow is a fresh signal."""
    repo.positions.open_position(make_intent(session="EQ-2026-07-24"), 5.00)
    repo.positions.close(
        repo.positions.open_positions()[0].position_id, 6.00, "TAKE_PROFIT"
    )
    _, created = repo.positions.open_position(
        make_intent(session="EQ-2026-07-27"), 5.00
    )
    assert created is True


def test_opposite_direction_is_a_different_signal():
    repo.positions.open_position(make_intent(), 5.00)
    other = make_intent()
    other.direction = Direction.LONG_PUT
    _, created = repo.positions.open_position(other, 4.00)
    assert created is True
    assert repo.positions.open_count_for("NVDA") == 2


def test_idempotency_key_shape():
    key = Position.make_idempotency_key(
        Market.CRYPTO_SPOT, "BTC-USD", Direction.LONG_SPOT, "CX-2026-07-25"
    )
    assert key == "CRYPTO_SPOT:BTC-USD:LONG_SPOT:CX-2026-07-25"


# ======================================================================
# Lifecycle
# ======================================================================
def test_open_mark_close_cycle():
    pos, _ = repo.positions.open_position(make_intent(), 5.00)
    assert pos.status is Status.OPEN

    marked = repo.positions.mark(pos.position_id, 6.00)
    assert marked.mark_price == 6.00
    assert marked.unrealized_pnl == pytest.approx(200.0)   # (6-5) * 2 * 100

    closed = repo.positions.close(pos.position_id, 6.50, "TAKE_PROFIT")
    assert closed.status is Status.CLOSED
    assert closed.realized_pnl == pytest.approx(300.0)
    assert repo.positions.open_count() == 0


def test_a_closed_position_cannot_reopen():
    pos, _ = repo.positions.open_position(make_intent(), 5.00)
    repo.positions.close(pos.position_id, 6.00, "TAKE_PROFIT")
    reloaded = repo.positions.get(pos.position_id)
    with pytest.raises(TransitionError):
        repo.positions._transition(None, reloaded, Status.OPEN)


def test_closing_twice_is_harmless():
    pos, _ = repo.positions.open_position(make_intent(), 5.00)
    first = repo.positions.close(pos.position_id, 6.00, "TAKE_PROFIT")
    second = repo.positions.close(pos.position_id, 9.99, "MANUAL")
    assert second.exit_price == first.exit_price == 6.00


def test_trail_high_water_only_rises():
    pos, _ = repo.positions.open_position(make_intent(price=5.00), 5.00)
    # trail arms at +30% => 6.50
    repo.positions.mark(pos.position_id, 6.00)
    assert repo.positions.get(pos.position_id).plan.trail_high_water is None

    repo.positions.mark(pos.position_id, 7.00)
    assert repo.positions.get(pos.position_id).plan.trail_high_water == 7.00

    repo.positions.mark(pos.position_id, 6.20)
    assert repo.positions.get(pos.position_id).plan.trail_high_water == 7.00


def test_every_transition_is_audited():
    pos, _ = repo.positions.open_position(make_intent(), 5.00)
    repo.positions.mark(pos.position_id, 7.00)
    repo.positions.request_close(pos.position_id, "MANUAL")
    repo.positions.close(pos.position_id, 7.00, "MANUAL")

    kinds = [e["event"] for e in repo.positions.events(pos.position_id)]
    assert kinds == ["OPENED", "TRAIL_RAISED", "CLOSE_REQUESTED", "CLOSED"]


def test_crypto_positions_can_be_fractional():
    intent = OrderIntent(
        market=Market.CRYPTO_SPOT, underlying="BTC-USD", instrument="BTC-USD",
        direction=Direction.LONG_SPOT, quantity=0.0142, multiplier=1,
        limit_price=68_500.0, session_key="CX-2026-07-25", scan_id="s1", score=74.0,
        plan=ExitPlan.build(68_500.0, stop_pct=0.05, target_pct=0.10,
                            trail_activate_pct=0.04, trail_giveback_pct=0.02,
                            max_hold_hours=24),
    )
    pos, created = repo.positions.open_position(intent, 68_600.0)
    assert created and pos.quantity == 0.0142
    closed = repo.positions.close(pos.position_id, 70_000.0, "TAKE_PROFIT")
    assert closed.realized_pnl == pytest.approx((70_000 - 68_600) * 0.0142, rel=1e-6)
