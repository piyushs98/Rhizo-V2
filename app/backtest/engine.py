"""
Walk-forward equity shares backtest using production scoring / risk / exits.

No lookahead: at day index i only bars[0:i+1] are visible.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.backtest.data import bar_on_or_before, bars_upto
from app.backtest.metrics import TradeRow, compute_metrics, Metrics
from app.broker.paper import PaperBroker
from app.config import Settings, settings as live_settings
from app.data.providers import Bar
from app.db import connection as db_connection
from app.db import repositories as repo
from app.domain.models import (
    Direction, ExitPlan, Market, OrderIntent, Position, Status, Verdict,
)
from app.engine import exit_rules, indicators as ind, regime as mkt_regime
from app.engine import risk, scoring

log = logging.getLogger("backtest.engine")


@dataclass
class BacktestConfig:
    starting_capital: float = 10_000.0
    risk_pct_per_trade: float = 0.08
    execute_threshold: float = 75.0
    # Signal on the close of T, fill the next session's open.
    # Stops are tested on the low, targets on the high. If both print, the
    # stop wins. A gap through the stop fills at the open.
    max_single_trade_pct: float = 0.25
    max_open_positions: int = 5
    max_positions_per_underlying: int = 1
    max_new_positions_per_day: int = 6
    reentry_cooldown_min: int = 0  # backtest: no multi-hour cooldown on daily bars
    market_regime_filter: bool = True
    best_of_n: bool = True
    stop_pct: float = 0.025
    target_pct: float = 0.050
    trail_activate_pct: float = 0.020
    trail_giveback_pct: float = 0.35
    max_hold_days: float = 10.0
    scale_out_half_at: float | None = None  # e.g. 0.15 then trail remainder
    min_trade_notional: float = 25.0
    warmup_bars: int = 60
    label: str = "baseline"
    # Optional hook: mutate bars visible at T (for lookahead test).
    bars_view: Callable[[str, list[Bar], datetime], list[Bar]] | None = None


@dataclass
class BacktestResult:
    metrics: Metrics
    trades: list[TradeRow]
    equity_curve: list[tuple[datetime, float]]
    config: BacktestConfig
    data_notes: str = ""
    avg_exposure_pct: float = 0.0


def _isolate_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="janus-bt-"))
    db_path = tmp / "bt.db"
    os.environ["DB_PATH"] = str(db_path)
    # Force new connection on this thread.
    conn = getattr(db_connection._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        db_connection._local.conn = None
    # Reload settings path is already bound; init_db uses settings.db_path
    # which was captured at import. Patch settings.db_path via object... frozen.
    # Work around: write to the env and re-bind connection path used by get_connection.
    # connection.get_connection reads settings.db_path each time for new conn.
    # Settings is frozen — monkeypatch module attr used at connect time.
    return db_path


_SETTINGS_STACK: list = []


def _patch_settings(cfg: BacktestConfig) -> Settings:
    """
    Temporarily replace the process-wide settings singleton (and the bound
    copies in risk/scoring/connection). Always pair with _restore_settings().
    """
    from app import config as config_mod
    from app.engine import risk as risk_mod
    from app.engine import scoring as scoring_mod

    # Snapshot every module that holds a settings binding.
    _SETTINGS_STACK.append({
        "config": config_mod.settings,
        "risk": risk_mod.settings,
        "scoring": scoring_mod.settings,
        "connection": getattr(db_connection, "settings", config_mod.settings),
    })

    base = config_mod.settings
    tmp = Path(tempfile.mkdtemp(prefix="janus-bt-"))
    db_path = str(tmp / "bt.db")
    new = replace(
        base,
        starting_capital=cfg.starting_capital,
        risk_pct_per_trade=cfg.risk_pct_per_trade,
        execute_threshold=cfg.execute_threshold,
        max_single_trade_pct=cfg.max_single_trade_pct,
        max_open_positions=cfg.max_open_positions,
        max_positions_per_underlying=cfg.max_positions_per_underlying,
        max_new_positions_per_day=cfg.max_new_positions_per_day,
        reentry_cooldown_min=cfg.reentry_cooldown_min,
        market_regime_filter=cfg.market_regime_filter,
        min_trade_notional=cfg.min_trade_notional,
        trading_enabled=True,
        dry_run=False,
        equity_instrument="shares",
        stop_loss_pct_shares=cfg.stop_pct,
        take_profit_pct_shares=cfg.target_pct,
        trail_activate_pct_shares=cfg.trail_activate_pct,
        trail_giveback_pct_shares=cfg.trail_giveback_pct,
        max_hold_hours_shares=cfg.max_hold_days * 24.0,
        db_path=db_path,
        log_dir=str(tmp / "logs"),
    )

    config_mod.settings = new
    risk_mod.settings = new
    scoring_mod.settings = new
    db_connection.settings = new
    conn = getattr(db_connection._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        db_connection._local.conn = None
    return new


def _restore_settings() -> None:
    if not _SETTINGS_STACK:
        return
    snap = _SETTINGS_STACK.pop()
    from app import config as config_mod
    from app.engine import risk as risk_mod
    from app.engine import scoring as scoring_mod
    config_mod.settings = snap["config"]
    risk_mod.settings = snap["risk"]
    scoring_mod.settings = snap["scoring"]
    db_connection.settings = snap["connection"]
    conn = getattr(db_connection._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        db_connection._local.conn = None


def _score_share(
    symbol: str,
    bars: list[Bar],
    *,
    spy_closes: list[float],
    news_bias: float = 0.0,
    threshold: float,
    regime_filter: bool,
) -> tuple[float, Direction | None, dict]:
    """Mirror EquitySharesAdapter scoring with only visible bars."""
    if len(bars) < 25:
        return 0.0, None, {"error": "short_history"}
    closes = [b.close for b in bars]
    bullish = ind.trend_score(closes) >= 0
    if not bullish:
        return 0.0, None, {"skip": "bearish_long_only"}

    direction = Direction.LONG_SHARE
    quote_change = 0.0
    if len(closes) >= 2 and closes[-2]:
        quote_change = (closes[-1] / closes[-2] - 1.0) * 100.0
    spy_chg = None
    if len(spy_closes) >= 2 and spy_closes[-2]:
        spy_chg = (spy_closes[-1] / spy_closes[-2] - 1.0) * 100.0

    liq = scoring.score_spot_liquidity(bars)
    tech = scoring.score_technical(bars, bullish=True)
    sent = scoring.score_sentiment(
        change_pct=quote_change,
        benchmark_change_pct=spy_chg,
        momentum_pct=ind.momentum_pct(closes, 10),
        volume_ratio=None,
        bullish=True,
        news_bias=news_bias,
    )
    card = scoring.compose(symbol, liq, tech, sent)
    detail = {
        "liq": liq[0], "tech": tech[0], "sent": sent[0], "total": card.total,
        "price": closes[-1],
    }

    if regime_filter and spy_closes:
        reg = mkt_regime.classify_spy_regime(spy_closes)
        detail["regime"] = reg.value
        block = mkt_regime.blocks_direction(reg, direction.value)
        if block:
            detail["blocked_by"] = "MARKET_REGIME"
            return card.total, None, detail

    if card.total < threshold:
        detail["blocked_by"] = "THRESHOLD"
        return card.total, None, detail
    return card.total, direction, detail


def share_bar_exit(
    pos: Position,
    bar: Bar,
    now: datetime,
    lock_pct: float,
) -> tuple[float, str] | None:
    """
    Intrabar exit for a long share. Returns (fill_price, reason) or None.

    Order assumed when the path is ambiguous: gap at the open first, then
    the stop (including a lock armed by this bar's high) before the target.
    That is the pessimistic reading of a bar that prints both.
    """
    plan = pos.plan
    if plan is None or not pos.entry_price or pos.entry_price <= 0:
        return None
    lock = exit_rules.lock_in_price(pos.entry_price, lock_pct)
    stop = plan.stop_price
    hw = plan.trail_high_water
    if lock and hw is not None and hw >= lock - 1e-12:
        stop = max(stop, lock)

    if bar.open <= stop:
        return bar.open, "STOP_LOSS"

    armed = stop
    if lock and bar.high >= lock:
        armed = max(stop, lock)
    if bar.low <= armed:
        return armed, "STOP_LOSS"

    if (
        plan.trail_activate_at is not None
        and plan.trail_giveback_pct > 0
        and bar.high >= plan.trail_activate_at
    ):
        hw_now = max(hw or 0.0, bar.high)
        trail_level = hw_now * (1 - plan.trail_giveback_pct)
        if trail_level > armed and bar.low <= trail_level:
            return trail_level, "TRAILING_STOP"

    if bar.high >= plan.target_price:
        return plan.target_price, "TAKE_PROFIT"
    if plan.time_stop_ts is not None and now >= plan.time_stop_ts:
        return bar.close, "TIME_STOP"
    return None


def _remember_lock_and_mark(pos: Position, bar: Bar, lock_pct: float) -> None:
    """Persist a lock armed by today's high, then mark the close."""
    lock = exit_rules.lock_in_price(pos.entry_price or 0.0, lock_pct)
    if lock and bar.high >= lock:
        repo.positions.raise_stop(pos.position_id, lock)
    repo.positions.mark(pos.position_id, bar.close)


def run_shares_backtest(
    series: dict[str, list[Bar]],
    *,
    cfg: BacktestConfig,
    spy_symbol: str = "SPY",
) -> BacktestResult:
    """
    series: symbol -> full daily bar list (will be sliced per day; never look ahead).
    """
    st = _patch_settings(cfg)
    try:
        return _run_shares_backtest_inner(series, cfg=cfg, spy_symbol=spy_symbol, st=st)
    finally:
        _restore_settings()


def _run_shares_backtest_inner(
    series: dict[str, list[Bar]],
    *,
    cfg: BacktestConfig,
    spy_symbol: str,
    st: Settings,
) -> BacktestResult:
    db_connection.init_db()
    # Force ledger capital
    from app.db.connection import execute, utcnow
    execute(
        "UPDATE ledger SET starting_capital=?, cash=?, peak_equity=?, updated_at=? WHERE id=1",
        (cfg.starting_capital, cfg.starting_capital, cfg.starting_capital, utcnow()),
    )

    broker = PaperBroker()
    symbols = [s for s in series if s != spy_symbol]
    if spy_symbol not in series:
        raise ValueError("SPY series required for regime / baseline context")

    # Trading calendar = SPY dates (session days only)
    calendar = [b.ts for b in series[spy_symbol]]
    if len(calendar) <= cfg.warmup_bars + 5:
        raise ValueError(
            f"insufficient history: {len(calendar)} SPY bars, need > {cfg.warmup_bars + 5}"
        )

    trades: list[TradeRow] = []
    equity_curve: list[tuple[datetime, float]] = []
    exposures: list[float] = []
    open_meta: dict[str, dict] = {}
    # (score, symbol, direction) scored on the prior close, filled next open.
    pending: list[tuple[float, str, Direction]] = []
    lock_pct = st.lock_in_profit_pct_shares

    def visible(sym: str, t: datetime) -> list[Bar]:
        full = series[sym]
        upto = bars_upto(full, t)
        if cfg.bars_view:
            return cfg.bars_view(sym, upto, t)
        return upto

    def bar_for(sym: str, t: datetime) -> Bar | None:
        vbars = visible(sym, t)
        return bar_on_or_before(vbars, t) if vbars else None

    def book_close(pos: Position, price: float, reason: str, t: datetime) -> None:
        fill = broker.sell(pos, price, reason)
        closed = repo.positions.close(pos.position_id, fill.price, reason, at=t)
        meta = open_meta.pop(pos.position_id, {})
        entry_ts = meta.get("entry_ts") or t
        hold_h = (
            (t - entry_ts).total_seconds() / 3600.0
            if isinstance(entry_ts, datetime) else 0.0
        )
        trades.append(TradeRow(
            open_date=meta.get("open_date", ""),
            close_date=t.date().isoformat(),
            symbol=pos.underlying,
            direction=pos.direction.value,
            entry=pos.entry_price or 0.0,
            exit=fill.price,
            reason=reason,
            pnl=closed.realized_pnl if closed else 0.0,
            hold_hours=round(hold_h, 2),
            score=meta.get("score", 0.0),
        ))

    for i, t in enumerate(calendar):
        if i < cfg.warmup_bars:
            continue

        # 1. Yesterday's close is today's open. No same-bar fill.
        for total, sym, direction in pending:
            if repo.positions.open_count() >= cfg.max_open_positions:
                break
            bar = bar_for(sym, t)
            if bar is None or bar.open <= 0:
                continue
            price = float(bar.open)
            skey = f"EQ-{t.date().isoformat()}"
            key = Position.make_idempotency_key(
                Market.EQUITY_SHARE, sym, direction, skey
            )
            decision = risk.check(
                market=Market.EQUITY_SHARE,
                underlying=sym,
                direction=direction,
                idempotency_key=key,
                entry_price=price,
                multiplier=1.0,
                whole_units=False,
                now=t,
            )
            if not decision.allowed:
                continue
            qty = risk.size_position(
                decision.max_notional, price, 1.0, whole_units=False
            )
            if qty <= 0:
                continue
            plan = ExitPlan.build(
                price,
                stop_pct=cfg.stop_pct,
                target_pct=cfg.target_pct,
                trail_activate_pct=cfg.trail_activate_pct,
                trail_giveback_pct=cfg.trail_giveback_pct,
                max_hold_hours=cfg.max_hold_days * 24.0,
                now=t,
            )
            intent = OrderIntent(
                market=Market.EQUITY_SHARE,
                underlying=sym,
                instrument=sym,
                direction=direction,
                quantity=qty,
                multiplier=1.0,
                limit_price=price,
                session_key=skey,
                scan_id=f"bt-{t.date().isoformat()}",
                score=total,
                plan=plan,
            )
            fill = broker.buy(intent)
            pos, created = repo.positions.open_position(intent, fill.price, at=t)
            if created:
                open_meta[pos.position_id] = {
                    "open_date": t.date().isoformat(),
                    "entry_ts": t,
                    "score": total,
                }
        pending = []

        # 2. Stops on the low, targets on the high, stop first if both.
        for pos in list(repo.positions.open_positions()):
            bar = bar_for(pos.underlying, t)
            if bar is None:
                continue
            hit = share_bar_exit(pos, bar, t, lock_pct)
            if hit is not None:
                book_close(pos, hit[0], hit[1], t)
            else:
                _remember_lock_and_mark(pos, bar, lock_pct)

        # 3. Score the close. The order waits for the next session's open.
        if i + 1 < len(calendar):
            spy_v = visible(spy_symbol, t)
            spy_closes = [b.close for b in spy_v]
            candidates: list[tuple[float, str, Direction]] = []
            for sym in symbols:
                if sym not in series:
                    continue
                total, direction, _detail = _score_share(
                    sym, visible(sym, t),
                    spy_closes=spy_closes,
                    threshold=cfg.execute_threshold,
                    regime_filter=cfg.market_regime_filter,
                )
                if direction is None:
                    continue
                candidates.append((total, sym, direction))
            if cfg.best_of_n:
                candidates.sort(key=lambda x: x[0], reverse=True)
            pending = candidates

        summary = risk.portfolio_summary()
        equity = float(summary["equity"])
        equity_curve.append((t, equity))
        if equity > 0:
            exposures.append(float(summary["open_value"]) / equity)

    # Flatten remainder at last close
    t_end = calendar[-1]
    for pos in list(repo.positions.open_positions()):
        vb = visible(pos.underlying, t_end)
        bar = bar_on_or_before(vb, t_end)
        if bar is None:
            continue
        fill = broker.sell(pos, bar.close, "TIME_STOP")
        closed = repo.positions.close(pos.position_id, fill.price, "TIME_STOP", at=t_end)
        meta = open_meta.pop(pos.position_id, {})
        trades.append(TradeRow(
            open_date=meta.get("open_date", ""),
            close_date=t_end.date().isoformat(),
            symbol=pos.underlying,
            direction=pos.direction.value,
            entry=pos.entry_price or 0.0,
            exit=fill.price,
            reason="TIME_STOP",
            pnl=closed.realized_pnl if closed else 0.0,
            hold_hours=0.0,
            score=meta.get("score", 0.0),
        ))

    final = risk.portfolio_summary()
    period_days = (calendar[-1] - calendar[cfg.warmup_bars]).total_seconds() / 86400.0
    metrics = compute_metrics(
        label=cfg.label,
        starting_capital=cfg.starting_capital,
        ending_equity=float(final["equity"]),
        trades=trades,
        equity_curve=equity_curve,
        period_days=period_days,
    )
    avg_exposure = (sum(exposures) / len(exposures)) if exposures else 0.0
    return BacktestResult(
        metrics=metrics,
        trades=trades,
        equity_curve=equity_curve,
        config=cfg,
        avg_exposure_pct=round(avg_exposure * 100.0, 2),
    )


def buy_and_hold(
    bars: list[Bar],
    *,
    starting_capital: float,
    warmup_bars: int = 60,
    label: str = "buy_hold",
) -> BacktestResult:
    if len(bars) <= warmup_bars + 1:
        raise ValueError("not enough bars for buy-and-hold")
    start_bar = bars[warmup_bars]
    end_bar = bars[-1]
    qty = starting_capital / start_bar.close
    # no fees for pure baseline (stated)
    end_eq = qty * end_bar.close
    trades = [TradeRow(
        open_date=start_bar.ts.date().isoformat(),
        close_date=end_bar.ts.date().isoformat(),
        symbol="HOLD",
        direction="LONG",
        entry=start_bar.close,
        exit=end_bar.close,
        reason="HOLD",
        pnl=end_eq - starting_capital,
        hold_hours=(end_bar.ts - start_bar.ts).total_seconds() / 3600.0,
        score=0.0,
    )]
    curve = []
    for b in bars[warmup_bars:]:
        curve.append((b.ts, qty * b.close))
    period_days = (end_bar.ts - start_bar.ts).total_seconds() / 86400.0
    metrics = compute_metrics(
        label=label,
        starting_capital=starting_capital,
        ending_equity=end_eq,
        trades=trades,
        equity_curve=curve,
        period_days=period_days,
    )
    return BacktestResult(metrics=metrics, trades=trades, equity_curve=curve,
                          config=BacktestConfig(label=label, starting_capital=starting_capital))
