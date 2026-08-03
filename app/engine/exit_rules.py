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
    1. stop loss (incl. lock-in floor once +LOCK_IN_PROFIT_PCT is reached)
    2. VWAP break       scalp floor (only when plan.scalp and vwap_floor set)
    3. trailing stop    lock in what the move gave you
    4. take profit      then take the win
    5. time stop        then stop paying theta for nothing
    6. session flatten  then respect the venue's clock
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import settings
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


def lock_in_price(entry: float, lock_pct: float | None = None) -> float | None:
    """Price at which the stop floor ratchets to lock in `lock_pct` gain."""
    pct = settings.lock_in_profit_pct if lock_pct is None else lock_pct
    if entry <= 0 or pct <= 0:
        return None
    return round(entry * (1.0 + pct), 6)


def effective_stop(
    position: Position,
    mark: float,
    *,
    lock_pct: float | None = None,
) -> float:
    """
    Live stop level: plan stop, raised to the lock-in price once the trade
    has reached +LOCK_IN_PROFIT_PCT (seen via mark or trail high water).
    """
    plan = position.plan
    if plan is None:
        return 0.0
    stop = plan.stop_price
    entry = position.entry_price or 0.0
    lock = lock_in_price(entry, lock_pct)
    if lock is None:
        return stop
    # Armed if mark is at/above lock, or we already printed a higher water mark.
    hw = plan.trail_high_water
    armed = mark >= lock or (hw is not None and hw >= lock) or stop >= lock - 1e-12
    if armed:
        return max(stop, lock)
    return stop


def evaluate(
    position: Position,
    mark: float,
    *,
    now: datetime | None = None,
    session_flatten: bool = False,
    mark_age_s: float | None = None,
    lock_pct: float | None = None,
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
    stop = effective_stop(position, mark, lock_pct=lock_pct)

    # --- 1. Hard stop (original plan stop or lock-in floor)
    if mark <= stop:
        lock = lock_in_price(entry, lock_pct)
        locked = lock is not None and stop >= lock - 1e-12 and lock > plan.stop_price + 1e-12
        label = "lock-in stop" if locked else "stop"
        return ExitSignal.close(
            ExitReason.STOP_LOSS,
            f"mark {mark:.4f} at or below {label} {stop:.4f} "
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
        if mark <= trail_level and trail_level > stop:
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
    if session_flatten and position.market in (
        Market.EQUITY_OPTION, Market.EQUITY_SHARE,
    ):
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
            f"stop {stop:.4f}"]
    lock = lock_in_price(entry, lock_pct)
    if lock is not None and stop >= lock - 1e-12:
        bits.append(f"lock-in {lock:.4f}")
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
