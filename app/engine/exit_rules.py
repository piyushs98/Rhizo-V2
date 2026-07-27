"""
Exit rules. Pure functions. No I/O, no model calls.

This module is the direct answer to the biggest operational gap in v1: the
CEO opened positions and nothing ever closed them, so realized PnL could
never be booked. The tracker that would have done it was fully written and
commented out of the boot path.

Here the exit plan is fixed at entry and evaluated by `evaluate()` on every
manage tick. The decision is a pure function of (position, mark, clock), so
it is deterministic, cheap, and completely unit-testable. A language model
can comment on a position; it cannot close one.

Precedence matters and is deliberate:
    1. stop loss        capital preservation first
    2. VWAP break       scalp floor (only when plan.scalp and vwap_floor set)
    3. trailing stop    lock in what the move gave you
    4. take profit      then take the win
    5. time stop        then stop paying theta for nothing
    6. session flatten  then respect the venue's clock
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.domain.models import ExitReason, Market, Position


@dataclass(frozen=True)
class ExitSignal:
    should_close: bool
    reason: ExitReason | None = None
    detail: str = ""

    @classmethod
    def hold(cls, detail: str = "") -> "ExitSignal":
        return cls(False, None, detail)

    @classmethod
    def close(cls, reason: ExitReason, detail: str) -> "ExitSignal":
        return cls(True, reason, detail)


MAX_MARK_AGE_S = 15 * 60      # a stale mark is not a valid basis for holding


def evaluate(
    position: Position,
    mark: float,
    *,
    now: datetime | None = None,
    session_flatten: bool = False,
    mark_age_s: float | None = None,
) -> ExitSignal:
    """Decide whether to close `position` at the current `mark`."""
    now = now or datetime.now(tz=timezone.utc)
    plan = position.plan

    if position.status.value not in ("OPEN", "CLOSING"):
        return ExitSignal.hold("not an open position")
    if plan is None:
        return ExitSignal.hold("no exit plan attached")
    if mark is None or mark <= 0:
        return ExitSignal.hold("no valid mark")

    entry = position.entry_price or 0.0
    pnl_pct = ((mark - entry) / entry * 100) if entry else 0.0

    # --- 1. Hard stop
    if mark <= plan.stop_price:
        return ExitSignal.close(
            ExitReason.STOP_LOSS,
            f"mark {mark:.4f} at or below stop {plan.stop_price:.4f} "
            f"({pnl_pct:+.1f}%)",
        )

    # --- 2. VWAP-break (scalp only). Second after the stop so a hard stop
    # still wins if both fire on the same tick.
    if plan.scalp and plan.vwap_floor is not None and plan.vwap_floor > 0:
        if mark < plan.vwap_floor:
            return ExitSignal.close(
                ExitReason.VWAP_BREAK,
                f"mark {mark:.4f} broke VWAP floor {plan.vwap_floor:.4f} "
                f"({pnl_pct:+.1f}%)",
            )

    # --- 3. Trailing stop, once armed
    if plan.trail_high_water is not None and plan.trail_giveback_pct > 0:
        trail_level = plan.trail_high_water * (1 - plan.trail_giveback_pct)
        if mark <= trail_level and trail_level > plan.stop_price:
            return ExitSignal.close(
                ExitReason.TRAILING_STOP,
                f"gave back {plan.trail_giveback_pct * 100:.0f}% from "
                f"{plan.trail_high_water:.4f} ({pnl_pct:+.1f}%)",
            )

    # --- 4. Take profit
    if mark >= plan.target_price:
        return ExitSignal.close(
            ExitReason.TAKE_PROFIT,
            f"mark {mark:.4f} at or above target {plan.target_price:.4f} "
            f"({pnl_pct:+.1f}%)",
        )

    # --- 5. Time stop
    if plan.time_stop_ts is not None and now >= plan.time_stop_ts:
        return ExitSignal.close(
            ExitReason.TIME_STOP,
            f"held past {plan.time_stop_ts:%Y-%m-%d %H:%M} UTC ({pnl_pct:+.1f}%)",
        )

    # --- 6. Session flatten (equities only; crypto has no close)
    if session_flatten and position.market is Market.EQUITY_OPTION:
        return ExitSignal.close(
            ExitReason.SESSION_FLATTEN,
            f"flattening before the bell ({pnl_pct:+.1f}%)",
        )

    # --- 7. Stale data. Not a close signal by itself, but worth surfacing.
    if mark_age_s is not None and mark_age_s > MAX_MARK_AGE_S:
        return ExitSignal.hold(
            f"mark is {mark_age_s / 60:.0f} min old - holding, but the price "
            f"feed needs attention"
        )

    bits = [f"{pnl_pct:+.1f}%", f"target {plan.target_price:.4f}",
            f"stop {plan.stop_price:.4f}"]
    if plan.scalp and plan.vwap_floor is not None:
        bits.append(f"vwap {plan.vwap_floor:.4f}")
    return ExitSignal.hold(" | ".join(bits))


def describe_plan(position: Position) -> str:
    """One-line human summary for alerts and the tape."""
    p = position.plan
    if not p:
        return "no exit plan"
    bits = [f"stop {p.stop_price:.4f}", f"target {p.target_price:.4f}"]
    if p.scalp and p.r_unit:
        bits.insert(0, f"scalp R={p.r_unit:.4f}")
    if p.vwap_floor is not None:
        bits.append(f"vwap floor {p.vwap_floor:.4f}")
    if p.trail_activate_at:
        bits.append(f"trail arms at {p.trail_activate_at:.4f}")
    if p.time_stop_ts:
        bits.append(f"time stop {p.time_stop_ts:%m-%d %H:%M}Z")
    return " | ".join(bits)
