"""
Walk-forward BTC spot backtest with production scoring + exit_rules + PaperBroker.

Uses CRYPTO_MAX_EXPOSURE as concurrent open notional cap via risk.check.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from app.backtest.data import bars_upto
from app.backtest.engine import (
    BacktestConfig, BacktestResult, _patch_settings, _restore_settings,
)
from app.backtest.metrics import TradeRow, compute_metrics
from app.broker.paper import PaperBroker
from app.data.providers import Bar
from app.db import connection as db_connection
from app.db import repositories as repo
from app.domain.models import Direction, ExitPlan, Market, OrderIntent, Position
from app.engine import exit_rules, indicators as ind, risk, scoring, scalping
from app.db.connection import execute, utcnow

log = logging.getLogger("backtest.crypto")


def run_crypto_backtest(
    bars: list[Bar],
    *,
    cfg: BacktestConfig,
    crypto_max_exposure: float = 1000.0,
    execute_threshold: float = 70.0,
    stop_pct: float = 0.030,
    target_pct: float = 0.055,
    trail_activate_pct: float = 0.025,
    trail_giveback_pct: float = 0.35,
    max_hold_hours: float = 24.0,
    use_scalp_gate: bool = True,
    symbol: str = "BTC-USD",
) -> BacktestResult:
    _patch_settings(cfg)
    try:
        return _run_crypto_inner(
            bars, cfg=cfg, crypto_max_exposure=crypto_max_exposure,
            execute_threshold=execute_threshold, stop_pct=stop_pct,
            target_pct=target_pct, trail_activate_pct=trail_activate_pct,
            trail_giveback_pct=trail_giveback_pct, max_hold_hours=max_hold_hours,
            use_scalp_gate=use_scalp_gate, symbol=symbol,
        )
    finally:
        _restore_settings()


def _run_crypto_inner(
    bars: list[Bar],
    *,
    cfg: BacktestConfig,
    crypto_max_exposure: float,
    execute_threshold: float,
    stop_pct: float,
    target_pct: float,
    trail_activate_pct: float,
    trail_giveback_pct: float,
    max_hold_hours: float,
    use_scalp_gate: bool,
    symbol: str,
) -> BacktestResult:
    from app import config as config_mod
    from app.engine import risk as risk_mod
    from app.engine import scoring as scoring_mod
    new = replace(
        config_mod.settings,
        crypto_max_exposure=crypto_max_exposure,
        execute_threshold_crypto=execute_threshold,
        stop_loss_pct_crypto=stop_pct,
        take_profit_pct_crypto=target_pct,
        trail_activate_pct_crypto=trail_activate_pct,
        trail_giveback_pct_crypto=trail_giveback_pct,
        max_hold_hours_crypto=max_hold_hours,
        starting_capital=cfg.starting_capital,
        risk_pct_per_trade=cfg.risk_pct_per_trade,
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

    db_connection.init_db()
    execute(
        "UPDATE ledger SET starting_capital=?, cash=?, peak_equity=?, updated_at=? WHERE id=1",
        (cfg.starting_capital, cfg.starting_capital, cfg.starting_capital, utcnow()),
    )

    broker = PaperBroker()
    trades: list[TradeRow] = []
    equity_curve: list[tuple[datetime, float]] = []
    open_meta: dict[str, dict] = {}
    warmup = max(cfg.warmup_bars, 48)

    for i in range(warmup, len(bars)):
        t = bars[i].ts
        window = bars_upto(bars, t)
        if len(window) < 30:
            continue
        price = window[-1].close

        # manage
        for pos in list(repo.positions.open_positions(Market.CRYPTO_SPOT)):
            repo.positions.mark(pos.position_id, price)
            pos2 = repo.positions.get(pos.position_id)
            if not pos2 or not pos2.plan:
                continue
            if pos2.plan.scalp:
                # refresh vwap floor when possible
                try:
                    floor = scalping.vwap(window, period=48)
                    if floor:
                        from app.db.connection import execute as ex
                        ex(
                            "UPDATE positions SET vwap_floor=? WHERE position_id=?",
                            (floor, pos2.position_id),
                        )
                        pos2 = repo.positions.get(pos2.position_id)
                except Exception:
                    pass
            sig = exit_rules.evaluate(pos2, price, now=t)
            if sig.should_close and sig.reason:
                fill = broker.sell(pos2, price, sig.reason.value)
                closed = repo.positions.close(pos2.position_id, fill.price, sig.reason.value)
                meta = open_meta.pop(pos2.position_id, {})
                trades.append(TradeRow(
                    open_date=meta.get("open_date", ""),
                    close_date=t.isoformat(timespec="seconds"),
                    symbol=symbol,
                    direction=pos2.direction.value,
                    entry=pos2.entry_price or 0.0,
                    exit=fill.price,
                    reason=sig.reason.value,
                    pnl=closed.realized_pnl if closed else 0.0,
                    hold_hours=0.0,
                    score=meta.get("score", 0.0),
                    market="CRYPTO_SPOT",
                ))

        # entry once per day-ish: every 6 hours to avoid overtrading same bar
        if i % 6 != 0:
            equity_curve.append((t, float(risk.portfolio_summary()["equity"])))
            continue

        closes = [b.close for b in window]
        change_pct = 0.0
        if len(closes) >= 2 and closes[-2]:
            change_pct = (closes[-1] / closes[-2] - 1.0) * 100.0
        liq = scoring.score_spot_liquidity(window)
        tech = scoring.score_technical(window, bullish=True)
        sent = scoring.score_sentiment(
            change_pct=change_pct, benchmark_change_pct=None,
            momentum_pct=ind.momentum_pct(closes, 24),
            volume_ratio=ind.volume_ratio(window, 24),
            bullish=True, news_bias=0.0,
        )
        card = scoring.compose(symbol, liq, tech, sent)
        if card.total < execute_threshold:
            equity_curve.append((t, float(risk.portfolio_summary()["equity"])))
            continue

        exit_plan = None
        if use_scalp_gate:
            ok, _diag = scalping.entry_gate(window)
            if not ok:
                equity_curve.append((t, float(risk.portfolio_summary()["equity"])))
                continue
            exit_plan = scalping.build_plan(price, window)
            if exit_plan is None:
                equity_curve.append((t, float(risk.portfolio_summary()["equity"])))
                continue
        else:
            exit_plan = ExitPlan.build(
                price, stop_pct=stop_pct, target_pct=target_pct,
                trail_activate_pct=trail_activate_pct,
                trail_giveback_pct=trail_giveback_pct,
                max_hold_hours=max_hold_hours, now=t,
            )

        skey = f"CX-{t.date().isoformat()}-{i // 6}"
        key = Position.make_idempotency_key(
            Market.CRYPTO_SPOT, symbol, Direction.LONG_SPOT, skey
        )
        decision = risk.check(
            market=Market.CRYPTO_SPOT, underlying=symbol,
            direction=Direction.LONG_SPOT, idempotency_key=key,
            entry_price=price, multiplier=1.0, whole_units=False, now=t,
        )
        if not decision.allowed:
            equity_curve.append((t, float(risk.portfolio_summary()["equity"])))
            continue
        qty = risk.size_position(
            decision.max_notional, price, 1.0, whole_units=False
        )
        if qty <= 0:
            equity_curve.append((t, float(risk.portfolio_summary()["equity"])))
            continue
        intent = OrderIntent(
            market=Market.CRYPTO_SPOT, underlying=symbol, instrument=symbol,
            direction=Direction.LONG_SPOT, quantity=qty, multiplier=1.0,
            limit_price=price, session_key=skey, scan_id=f"cbt-{i}",
            score=card.total, plan=exit_plan,
        )
        fill = broker.buy(intent)
        pos, created = repo.positions.open_position(intent, fill.price)
        if created:
            open_meta[pos.position_id] = {
                "open_date": t.isoformat(timespec="seconds"),
                "score": card.total,
            }

        equity_curve.append((t, float(risk.portfolio_summary()["equity"])))

    # flatten
    if bars:
        t = bars[-1].ts
        px = bars[-1].close
        for pos in list(repo.positions.open_positions(Market.CRYPTO_SPOT)):
            fill = broker.sell(pos, px, "TIME_STOP")
            closed = repo.positions.close(pos.position_id, fill.price, "TIME_STOP")
            meta = open_meta.pop(pos.position_id, {})
            trades.append(TradeRow(
                open_date=meta.get("open_date", ""),
                close_date=t.isoformat(timespec="seconds"),
                symbol=symbol, direction=pos.direction.value,
                entry=pos.entry_price or 0.0, exit=fill.price,
                reason="TIME_STOP",
                pnl=closed.realized_pnl if closed else 0.0,
                hold_hours=0.0, score=meta.get("score", 0.0),
                market="CRYPTO_SPOT",
            ))

    final = risk.portfolio_summary()
    period_days = (bars[-1].ts - bars[warmup].ts).total_seconds() / 86400.0
    metrics = compute_metrics(
        label=cfg.label or "crypto",
        starting_capital=cfg.starting_capital,
        ending_equity=float(final["equity"]),
        trades=trades,
        equity_curve=equity_curve,
        period_days=period_days,
    )
    return BacktestResult(
        metrics=metrics, trades=trades, equity_curve=equity_curve, config=cfg,
    )
