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


def _session_start_utc() -> str:
    """00:00 ET today, expressed in UTC ISO - the daily-limit boundary."""
    from app.clock import ET
    now = datetime.now(tz=ET)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def open_market_value(market: Market | None = None) -> float:
    """Mark-to-market value of open positions, optionally filtered by market."""
    total = 0.0
    for p in repo.positions.open_positions(market=market):
        px = p.mark_price if p.mark_price is not None else (p.entry_price or 0.0)
        total += px * p.quantity * p.multiplier
    return round(total, 2)


def account_equity() -> float:
    led = repo.ledger.get()
    return round(led.get("cash", 0.0) + open_market_value(), 2)


def _bucket_budget(
    market: Market, *, cash: float, equity: float
) -> tuple[float, float, str]:
    """
    Distinct capital buckets for equity vs crypto.

    Crypto sizes against CRYPTO_ALLOCATION only. Equity sizes against the rest
    of the account, and leaves the unfilled crypto allocation reserved in cash
    so the night desk is not starved by daytime fills.
    """
    crypto_open = open_market_value(Market.CRYPTO_SPOT)
    total_open = open_market_value()
    equity_open = max(0.0, total_open - crypto_open)
    crypto_alloc = max(0.0, settings.crypto_allocation)

    if market is Market.CRYPTO_SPOT:
        base = crypto_alloc
        free = max(0.0, base - crypto_open)
        free = min(free, cash)
        return base, free, "crypto"

    # Equity desk (options or shares)
    base = max(0.0, equity - crypto_alloc)
    free_capacity = max(0.0, base - equity_open)
    crypto_cash_reserve = max(0.0, crypto_alloc - crypto_open)
    cash_for_equity = max(0.0, cash - crypto_cash_reserve)
    free = min(free_capacity, cash_for_equity)
    return base, free, "equity"


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
    # The database enforces this too, via a UNIQUE constraint. Checking here
    # as well means we can report it cleanly instead of catching an error.
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
    opened_today = repo.positions.opened_since(_session_start_utc())
    if opened_today >= settings.max_new_positions_per_day:
        return RiskDecision.block(
            "DAILY_TRADE_CAP",
            f"{opened_today} opened today; limit is "
            f"{settings.max_new_positions_per_day}.",
        )

    # --- 6. Daily loss limit ----------------------------------------------
    led = repo.ledger.get()
    start_capital = led.get("starting_capital", settings.starting_capital)
    realized_today = repo.positions.realized_pnl_since(_session_start_utc())
    limit = -abs(start_capital * settings.daily_loss_limit_pct)
    if settings.daily_loss_limit_pct > 0 and realized_today <= limit:
        return RiskDecision.block(
            "DAILY_LOSS_LIMIT",
            f"Down {realized_today:,.0f} today against a "
            f"{limit:,.0f} limit. No new positions until tomorrow.",
        )

    # --- 7. Capital available (bucketed equity vs crypto) -----------------
    cash = led.get("cash", 0.0)
    equity = cash + open_market_value()
    base, free, bucket = _bucket_budget(market, cash=cash, equity=equity)
    budget = min(base * settings.risk_pct_per_trade, free)

    # The smallest position this market can express. For options that is one
    # whole contract; for spot/shares it is the configured minimum notional.
    unit_cost = entry_price * multiplier
    min_ticket = unit_cost if whole_units else settings.min_trade_notional

    # Options: never let one contract consume more than MAX_SINGLE_TRADE_PCT
    # of equity. Then, if the base risk budget cannot afford one contract but
    # the hard cap can, raise the per-trade budget just enough to buy one.
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
        if min_ticket > budget and unit_cost <= free and unit_cost <= max_single:
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
            f"{detail} {min_ticket:,.2f}; the {bucket} per-trade budget is "
            f"{budget:,.2f} (base {base:,.2f}).",
        )
    if min_ticket > free:
        return RiskDecision.block(
            "INSUFFICIENT_CASH",
            f"Need {min_ticket:,.2f}, have {free:,.2f} free in the "
            f"{bucket} bucket (cash {cash:,.2f}).",
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
    open_value = open_market_value()
    equity = led.get("cash", 0.0) + open_value
    start = led.get("starting_capital", settings.starting_capital)
    peak = max(led.get("peak_equity", start), equity)
    unrealized = sum(p.unrealized_pnl for p in open_positions)
    closed = led.get("trades_closed", 0)
    crypto_open = open_market_value(Market.CRYPTO_SPOT)

    return {
        "cash": round(led.get("cash", 0.0), 2),
        "open_value": open_value,
        "equity": round(equity, 2),
        "starting_capital": start,
        "crypto_allocation": settings.crypto_allocation,
        "crypto_open": crypto_open,
        "crypto_free": round(max(0.0, settings.crypto_allocation - crypto_open), 2),
        "equity_instrument": settings.equity_instrument,
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
