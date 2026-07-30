"""
Pre-trade risk gates.

v1 had a virtual ledger and no risk framework at all: no position sizing, no
cap on concurrent positions, no per-underlying limit, no daily loss limit, no
re-entry cooldown. Combined with the missing idempotency check, that is why
every scan stacked another contract onto the same name.

Every gate here returns a named refusal, and that name is written to
`scan_results.blocked_by` so the dashboard can show you exactly which rule
stopped a trade. "Why didn't it fire" should never be a mystery.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import repositories as repo
from app.domain.models import Direction, Market, Position

log = logging.getLogger("risk")


@dataclass
class RiskDecision:
    allowed: bool
    gate: str = ""
    reason: str = ""
    max_notional: float = 0.0

    @classmethod
    def ok(cls, max_notional: float) -> "RiskDecision":
        return cls(True, max_notional=max_notional)

    @classmethod
    def block(cls, gate: str, reason: str) -> "RiskDecision":
        return cls(False, gate=gate, reason=reason)


def _session_start_utc(now: datetime | None = None) -> str:
    """00:00 ET on the given day (or wall-clock today), as UTC ISO."""
    from app.clock import ET
    if now is None:
        local = datetime.now(tz=ET)
    else:
        local = now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=timezone.utc).astimezone(ET)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def open_market_value(market: Market | None = None) -> float:
    """Mark-to-market value of open positions, optionally filtered by market."""
    total = 0.0
    for p in repo.positions.open_positions(market=market):
        px = p.mark_price if p.mark_price is not None else (p.entry_price or 0.0)
        total += px * p.quantity * p.multiplier
    return round(total, 2)


def equity_open_notional() -> float:
    """Open MTM of equity desks (options + shares), not crypto."""
    total = 0.0
    for m in (Market.EQUITY_OPTION, Market.EQUITY_SHARE):
        total += open_market_value(m)
    return round(total, 2)


def crypto_open_notional() -> float:
    """Open MTM crypto notional (concurrent exposure, not cumulative volume)."""
    return open_market_value(Market.CRYPTO_SPOT)


def account_equity() -> float:
    led = repo.ledger.get()
    return round(led.get("cash", 0.0) + open_market_value(), 2)


def check(
    *,
    market: Market,
    underlying: str,
    direction: Direction,
    idempotency_key: str,
    entry_price: float,
    multiplier: float,
    whole_units: bool = True,
    now: datetime | None = None,
) -> RiskDecision:
    """
    Run every gate in order, cheapest and most decisive first.
    Returns the maximum notional permitted if all gates pass.

    Capital is one shared pool. CRYPTO_MAX_EXPOSURE is a hard ceiling on
    concurrent crypto MTM notional — not a reserved allocation.
    """
    now = now or datetime.now(tz=timezone.utc)

    # --- 0. Master switches ------------------------------------------------
    if not settings.trading_enabled:
        return RiskDecision.block("TRADING_DISABLED",
                                  "Trading is switched off in configuration.")
    if repo.kv.get_bool("halted", False):
        why = repo.kv.get("halt_reason", "halted from the dashboard")
        return RiskDecision.block("HALTED", why)

    # --- 1. Duplicate signal ----------------------------------------------
    if repo.positions.by_key(idempotency_key) is not None:
        return RiskDecision.block(
            "DUPLICATE",
            "Already holding this signal for the current session.",
        )

    # --- 2. Per-underlying concentration ----------------------------------
    held = repo.positions.open_count_for(underlying)
    if held >= settings.max_positions_per_underlying:
        return RiskDecision.block(
            "PER_UNDERLYING_CAP",
            f"{held} open in {underlying}; limit is "
            f"{settings.max_positions_per_underlying}.",
        )

    # --- 3. Total concurrent positions ------------------------------------
    total_open = repo.positions.open_count()
    if total_open >= settings.max_open_positions:
        return RiskDecision.block(
            "MAX_OPEN",
            f"{total_open} positions open; limit is {settings.max_open_positions}.",
        )

    # --- 4. Re-entry cooldown ---------------------------------------------
    last_close = repo.positions.last_close_ts(underlying)
    if last_close:
        cooldown = timedelta(minutes=settings.reentry_cooldown_min)
        elapsed = now - last_close
        if elapsed < cooldown:
            mins = int((cooldown - elapsed).total_seconds() // 60)
            return RiskDecision.block(
                "COOLDOWN",
                f"Closed {underlying} recently; {mins} min of cooldown left.",
            )

    # --- 5. New positions per day -----------------------------------------
    # Bound the "day" to `now` so walk-forward backtests do not collapse
    # every simulated day into the wall-clock calendar date.
    session_start = _session_start_utc(now)
    opened_today = repo.positions.opened_since(session_start)
    if opened_today >= settings.max_new_positions_per_day:
        return RiskDecision.block(
            "DAILY_TRADE_CAP",
            f"{opened_today} opened today; limit is "
            f"{settings.max_new_positions_per_day}.",
        )

    # --- 6. Daily loss limit ----------------------------------------------
    led = repo.ledger.get()
    start_capital = led.get("starting_capital", settings.starting_capital)
    realized_today = repo.positions.realized_pnl_since(session_start)
    limit = -abs(start_capital * settings.daily_loss_limit_pct)
    if settings.daily_loss_limit_pct > 0 and realized_today <= limit:
        return RiskDecision.block(
            "DAILY_LOSS_LIMIT",
            f"Down {realized_today:,.0f} today against a "
            f"{limit:,.0f} limit. No new positions until tomorrow.",
        )

    # --- 7. Shared capital pool -------------------------------------------
    cash = float(led.get("cash", 0.0))
    eq_open = equity_open_notional()
    cx_open = crypto_open_notional()
    equity = cash + eq_open + cx_open
    risk_budget = equity * settings.risk_pct_per_trade
    budget = min(risk_budget, cash)

    unit_cost = entry_price * multiplier
    min_ticket = unit_cost if whole_units else settings.min_trade_notional

    # --- 7a. Crypto concurrent exposure ceiling (MTM open only) ------------
    if market is Market.CRYPTO_SPOT:
        cap = max(0.0, settings.crypto_max_exposure)
        headroom = max(0.0, cap - cx_open)
        # Cap gate: even the smallest ticket would breach concurrent exposure.
        if min_ticket > headroom + 1e-9:
            return RiskDecision.block(
                "CRYPTO_EXPOSURE_CAP",
                f"Crypto open notional is {cx_open:,.2f}; headroom "
                f"{headroom:,.2f} against CRYPTO_MAX_EXPOSURE "
                f"{cap:,.2f}. Adding {min_ticket:,.2f} would breach the cap. "
                f"(Closed scalps do not count — only concurrent open MTM.)",
            )
        # Cash starved by equity (or anything else): distinct from the cap.
        if min_ticket > cash + 1e-9:
            return RiskDecision.block(
                "INSUFFICIENT_CASH",
                f"Need {min_ticket:,.2f} for crypto, have {cash:,.2f} cash. "
                f"Open equity notional {eq_open:,.2f} is holding capital "
                f"(crypto open {cx_open:,.2f}, headroom under cap "
                f"{headroom:,.2f}). Equity ate the account — not a cap hit.",
            )
        # Size against shared risk budget, cash, and remaining exposure headroom.
        budget = min(budget, headroom)

    # --- 7b. Options single-name hard ceiling + 1-contract floor ----------
    if whole_units and market is Market.EQUITY_OPTION and equity > 0:
        max_single = equity * settings.max_single_trade_pct
        if unit_cost > max_single + 1e-9:
            return RiskDecision.block(
                "MAX_SINGLE_TRADE",
                f"One contract costs {unit_cost:,.2f} "
                f"({unit_cost / equity * 100:.1f}% of equity "
                f"{equity:,.2f}); MAX_SINGLE_TRADE_PCT is "
                f"{settings.max_single_trade_pct * 100:.0f}%. "
                f"Refusing — one option trade must not dominate the account.",
            )
        if min_ticket > budget and unit_cost <= cash and unit_cost <= max_single:
            log.info(
                "options size floor: raising budget from %.2f to %.2f "
                "to afford 1 contract on %s (%.1f%% of equity)",
                budget, unit_cost, underlying, unit_cost / equity * 100,
            )
            budget = unit_cost

    if min_ticket > budget:
        detail = ("One contract costs" if whole_units
                  else "The minimum ticket is")
        return RiskDecision.block(
            "SIZE_TOO_LARGE",
            f"{detail} {min_ticket:,.2f}; the per-trade budget is "
            f"{budget:,.2f} (risk {settings.risk_pct_per_trade * 100:.1f}% "
            f"of equity {equity:,.2f}, cash {cash:,.2f}).",
        )
    if min_ticket > cash:
        # Equity path (or any market) when cash is the binding constraint.
        return RiskDecision.block(
            "INSUFFICIENT_CASH",
            f"Need {min_ticket:,.2f}, have {cash:,.2f} cash. "
            f"Open equity notional {eq_open:,.2f}, open crypto "
            f"{cx_open:,.2f}.",
        )

    return RiskDecision.ok(max_notional=budget)


def size_position(max_notional: float, entry_price: float,
                  multiplier: float, *, whole_units: bool) -> float:
    """
    How many units the budget buys.

    Options must be whole contracts. Shares/crypto can be fractional, rounded
    to 8 decimals.
    """
    if entry_price <= 0 or multiplier <= 0:
        return 0.0
    raw = max_notional / (entry_price * multiplier)
    if whole_units:
        return float(int(raw))
    qty = round(raw, 8)
    # Never place a dust ticket: fees would exceed any realistic edge.
    if qty * entry_price * multiplier < settings.min_trade_notional:
        return 0.0
    return qty


def portfolio_summary() -> dict:
    led = repo.ledger.get()
    open_positions = repo.positions.open_positions()
    eq_open = equity_open_notional()
    cx_open = crypto_open_notional()
    open_value = round(eq_open + cx_open, 2)
    cash = float(led.get("cash", 0.0))
    equity = cash + open_value
    start = led.get("starting_capital", settings.starting_capital)
    peak = max(led.get("peak_equity", start), equity)
    unrealized = sum(p.unrealized_pnl for p in open_positions)
    closed = led.get("trades_closed", 0)
    cx_cap = settings.crypto_max_exposure
    cx_headroom = round(max(0.0, cx_cap - cx_open), 2)

    return {
        "cash": round(cash, 2),
        "open_value": open_value,
        "open_equity_notional": eq_open,
        "open_crypto_notional": cx_open,
        "crypto_max_exposure": cx_cap,
        "crypto_headroom": cx_headroom,
        "equity": round(equity, 2),
        "starting_capital": start,
        "equity_instrument": settings.equity_instrument,
        "risk_pct_per_trade": settings.risk_pct_per_trade,
        "realized_pnl": round(led.get("realized_pnl", 0.0), 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(led.get("realized_pnl", 0.0) + unrealized, 2),
        "return_pct": round((equity / start - 1) * 100, 2) if start else 0.0,
        "drawdown_pct": round((equity / peak - 1) * 100, 2) if peak else 0.0,
        "peak_equity": round(peak, 2),
        "open_count": len(open_positions),
        "max_open": settings.max_open_positions,
        "trades_opened": led.get("trades_opened", 0),
        "trades_closed": closed,
        "wins": led.get("wins", 0),
        "losses": led.get("losses", 0),
        "win_rate": round(led.get("wins", 0) / closed * 100, 1) if closed else None,
        "realized_today": round(
            repo.positions.realized_pnl_since(_session_start_utc()), 2
        ),
        "daily_loss_limit": round(-abs(start * settings.daily_loss_limit_pct), 2),
        "halted": repo.kv.get_bool("halted", False),
        "halt_reason": repo.kv.get("halt_reason", ""),
    }
