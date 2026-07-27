"""
Position manager. Runs on a fast cadence, independent of the scanner.

This is the half of the system v1 was missing in production. There, the CEO
opened paper trades and the tracker that would have closed them was
commented out of the boot path, so every position stayed open forever and
realized PnL could never be booked.

Here, managing positions is not a separate agent that can be switched off.
It runs on every engine tick, before scanning, and it is the first thing the
scheduler calls. Entry and exit are the same loop.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.broker.base import Broker
from app.clock import SessionState
from app.config import settings
from app.data.providers import DataUnavailable
from app.db import repositories as repo
from app.domain.models import ExitReason, Market, Position, Status
from app.engine import exit_rules, risk
from app.markets.adapters import get_adapter
from app.notify import discord
from app.resilience.circuit_breaker import BreakerOpen
from app.resilience.timeouts import CallTimeout

log = logging.getLogger("positions")

# How close to the bell before we flatten equity positions, if enabled.
FLATTEN_WINDOW_S = 10 * 60


def manage_all(state: SessionState, broker: Broker) -> dict:
    """
    Re-mark every open position and act on any exit signal.

    Each position is isolated: a data failure on one never prevents another
    from being managed. Returns a small summary for the heartbeat detail.
    """
    open_positions = repo.positions.open_positions()
    if not open_positions:
        _snapshot()
        return {"managed": 0, "closed": 0, "stale": 0}

    closed = stale = 0
    flatten = _should_flatten(state)

    for pos in open_positions:
        try:
            mark = _mark(pos)
        except (DataUnavailable, CallTimeout, BreakerOpen) as exc:
            stale += 1
            log.warning("could not mark %s (%s): %s",
                        pos.underlying, pos.instrument, exc)
            _check_stale(pos)
            continue
        except Exception:
            stale += 1
            log.exception("unexpected error marking %s", pos.instrument)
            continue

        updated = repo.positions.mark(pos.position_id, mark)
        if updated is None:
            continue

        signal = exit_rules.evaluate(updated, mark, session_flatten=flatten)
        if signal.should_close and signal.reason:
            if _close(updated, mark, signal.reason, signal.detail, broker):
                closed += 1

    _snapshot()
    return {"managed": len(open_positions), "closed": closed, "stale": stale}


# ---------------------------------------------------------------- internals
def _mark(pos: Position) -> float:
    adapter = get_adapter(pos.market)
    return adapter.mark(pos.instrument, pos.underlying)


def _should_flatten(state: SessionState) -> bool:
    if not settings.flatten_equity_at_close:
        return False
    if state.regime.value != "EQUITY":
        return False
    return state.seconds_to_handoff <= FLATTEN_WINDOW_S


def _check_stale(pos: Position) -> None:
    """A position we cannot price is an operational problem. Say so, once."""
    if pos.mark_ts is None:
        return
    age = (datetime.now(tz=timezone.utc) - pos.mark_ts).total_seconds()
    if age > exit_rules.MAX_MARK_AGE_S:
        discord.warn(
            f"No fresh price for **{pos.underlying}** (`{pos.instrument}`) "
            f"in {age / 60:.0f} minutes. The position is still open and the "
            f"stop cannot be evaluated.",
            channel="positions",
            dedupe_key=f"stale:{pos.position_id}",
            cooldown_s=1800,
        )


def _close(pos: Position, mark: float, reason: ExitReason,
           detail: str, broker: Broker) -> bool:
    try:
        repo.positions.request_close(pos.position_id, reason.value)
        fill = broker.sell(pos, mark, reason.value)
        final = repo.positions.close(
            pos.position_id, fill.price, reason.value, fees=fill.fees
        )
    except Exception:
        log.exception("failed to close %s", pos.position_id)
        discord.critical(
            f"Could not close **{pos.underlying}** (`{pos.instrument}`). "
            f"The position is still on the book. Check the engine log.",
            channel="positions",
            dedupe_key=f"closefail:{pos.position_id}",
        )
        return False

    if final is None:
        return False

    won = final.realized_pnl > 0
    discord.send(
        f"**Closed {final.underlying}** \u00b7 {reason.value}\n"
        f"`{final.instrument}`\n"
        f"{final.quantity:g} @ {final.exit_price:,.4f} "
        f"(entry {final.entry_price:,.4f})\n"
        f"Realized **{final.realized_pnl:+,.2f}** "
        f"({final.pnl_pct(final.exit_price):+.1f}%)\n"
        f"{detail}",
        "INFO" if won else "WARN",
        channel="execution",
    )
    return True


def _snapshot() -> None:
    """Append a point to the equity curve. Cheap; drives the dashboard chart."""
    try:
        repo.ledger.snapshot(risk.open_market_value(), repo.positions.open_count())
    except Exception:
        log.exception("equity snapshot failed")


# ------------------------------------------------------------ manual closes
def close_manually(position_id: str, broker: Broker, note: str = "") -> str:
    """Called by the command processor when you press Close on the dashboard."""
    pos = repo.positions.get(position_id)
    if pos is None:
        return f"No position {position_id}."
    if pos.status is Status.CLOSED:
        return f"{pos.underlying} is already closed."

    try:
        mark = _mark(pos)
    except Exception as exc:
        mark = pos.mark_price or pos.entry_price or 0.0
        log.warning("manual close using last known mark for %s: %s",
                    pos.instrument, exc)

    ok = _close(pos, mark, ExitReason.MANUAL, note or "closed from dashboard", broker)
    return (f"Closed {pos.underlying} at {mark:,.4f}." if ok
            else f"Could not close {pos.underlying}.")


def flatten_all(broker: Broker, reason: ExitReason = ExitReason.MANUAL) -> str:
    positions = repo.positions.open_positions()
    if not positions:
        return "Nothing open."
    done = 0
    for pos in positions:
        try:
            mark = _mark(pos)
        except Exception:
            mark = pos.mark_price or pos.entry_price or 0.0
        if _close(pos, mark, reason, "flatten all", broker):
            done += 1
    return f"Closed {done} of {len(positions)} positions."
