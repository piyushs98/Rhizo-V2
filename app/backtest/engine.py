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
        volume_ratio=ind.volume_ratio(bars, 20),
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
    # Track open meta for trade log
    open_meta: dict[str, dict] = {}

    def visible(sym: str, t: datetime) -> list[Bar]:
        full = series[sym]
        upto = bars_upto(full, t)
        if cfg.bars_view:
            return cfg.bars_view(sym, upto, t)
        return upto

    for i, t in enumerate(calendar):
        if i < cfg.warmup_bars:
            continue

        # --- manage opens (exit_rules on today's mark) ---
        for pos in list(repo.positions.open_positions()):
            vbars = visible(pos.underlying, t)
            bar = bar_on_or_before(vbars, t) if vbars else None
            if bar is None:
                continue
            mark = bar.close
            # trail high-water via mark()
            repo.positions.mark(pos.position_id, mark)
            pos2 = repo.positions.get(pos.position_id)
            if pos2 is None or pos2.plan is None:
                continue

            # scale-out half at target intermediate
            if cfg.scale_out_half_at and pos2.quantity > 0 and pos2.entry_price:
                thr = pos2.entry_price * (1 + cfg.scale_out_half_at)
                meta = open_meta.get(pos2.position_id, {})
                if mark >= thr and not meta.get("scaled"):
                    half = pos2.quantity / 2.0
                    if half * mark >= st.min_trade_notional:
                        # close half via sell + shrink remaining in DB is complex;
                        # approximate: book half PnL and halve quantity in place
                        fill = broker.sell(
                            replace(pos2, quantity=half), mark, "SCALE_OUT"
                        )
                        # manual quantity shrink
                        from app.db.connection import execute as ex
                        ex(
                            "UPDATE positions SET quantity=?, entry_notional=? WHERE position_id=?",
                            (pos2.quantity - half,
                             (pos2.entry_price or 0) * (pos2.quantity - half),
                             pos2.position_id),
                        )
                        open_meta.setdefault(pos2.position_id, {})["scaled"] = True
                        trades.append(TradeRow(
                            open_date=meta.get("open_date", ""),
                            close_date=t.date().isoformat(),
                            symbol=pos2.underlying,
                            direction=pos2.direction.value,
                            entry=pos2.entry_price or 0.0,
                            exit=fill.price,
                            reason="SCALE_OUT",
                            pnl=round(
                                (fill.price - (pos2.entry_price or 0.0)) * half
                                - fill.fees, 2
                            ),
                            hold_hours=0.0,
                            score=meta.get("score", 0.0),
                        ))
                        pos2 = repo.positions.get(pos2.position_id)
                        if pos2 is None:
                            continue

            sig = exit_rules.evaluate(pos2, mark, now=t, session_flatten=False)
            if sig.should_close and sig.reason:
                fill = broker.sell(pos2, mark, sig.reason.value)
                closed = repo.positions.close(
                    pos2.position_id, fill.price, sig.reason.value, at=t,
                )
                meta = open_meta.pop(pos2.position_id, {})
                entry_ts = meta.get("entry_ts") or t
                hold_h = (t - entry_ts).total_seconds() / 3600.0 if isinstance(entry_ts, datetime) else 0.0
                trades.append(TradeRow(
                    open_date=meta.get("open_date", ""),
                    close_date=t.date().isoformat(),
                    symbol=pos2.underlying,
                    direction=pos2.direction.value,
                    entry=pos2.entry_price or 0.0,
                    exit=fill.price,
                    reason=sig.reason.value,
                    pnl=closed.realized_pnl if closed else 0.0,
                    hold_hours=round(hold_h, 2),
                    score=meta.get("score", 0.0),
                ))

        # --- entries ---
        spy_v = visible(spy_symbol, t)
        spy_closes = [b.close for b in spy_v]
        candidates: list[tuple[float, str, Direction, dict]] = []
        for sym in symbols:
            if sym not in series:
                continue
            vb = visible(sym, t)
            total, direction, detail = _score_share(
                sym, vb,
                spy_closes=spy_closes,
                threshold=cfg.execute_threshold,
                regime_filter=cfg.market_regime_filter,
            )
            if direction is None:
                continue
            candidates.append((total, sym, direction, detail))

        if cfg.best_of_n:
            candidates.sort(key=lambda x: x[0], reverse=True)
        # else first-past-the-post: keep universe order as built

        for total, sym, direction, detail in candidates:
            if repo.positions.open_count() >= cfg.max_open_positions:
                break
            price = float(detail["price"])
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

        # mark equity curve
        summary = risk.portfolio_summary()
        equity_curve.append((t, float(summary["equity"])))

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
    return BacktestResult(
        metrics=metrics,
        trades=trades,
        equity_curve=equity_curve,
        config=cfg,
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
