"""
Lookahead invariant: results up to T must be identical when all data after T
is replaced with garbage.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backtest.data import bars_upto
from app.backtest.engine import BacktestConfig, run_shares_backtest
from app.data.providers import Bar


def _synth(symbol: str, n: int = 120, start: float = 100.0) -> list[Bar]:
    bars = []
    px = start
    t0 = datetime(2024, 1, 2, tzinfo=timezone.utc)
    for i in range(n):
        # gentle uptrend with weekly dip
        px = px * (1.002 if i % 7 else 0.995)
        bars.append(Bar(
            ts=t0 + timedelta(days=i),
            open=px * 0.999, high=px * 1.01, low=px * 0.99,
            close=px, volume=1_000_000 + i * 1000,
        ))
    return bars


def test_bars_upto_is_strict():
    bars = _synth("X", 10)
    t = bars[4].ts
    upto = bars_upto(bars, t)
    assert len(upto) == 5
    assert all(b.ts <= t for b in upto)
    assert upto[-1].ts == t


def test_no_lookahead_garbage_future():
    """
    Run A on clean data. Run B with all bars after T replaced by nonsense.
    Trades/equity up to T must match.
    """
    series = {
        "SPY": _synth("SPY", 100, 400.0),
        "AAA": _synth("AAA", 100, 50.0),
        "BBB": _synth("BBB", 100, 80.0),
    }
    cfg = BacktestConfig(
        starting_capital=10_000.0,
        risk_pct_per_trade=0.08,
        execute_threshold=50.0,  # looser so synthetic can fire
        market_regime_filter=False,
        best_of_n=True,
        reentry_cooldown_min=0,
        warmup_bars=40,
        label="lookahead_a",
        stop_pct=0.05,
        target_pct=0.10,
    )
    res_a = run_shares_backtest(series, cfg=cfg)

    # Pick T = midpoint of equity curve
    assert res_a.equity_curve, "no equity curve from clean run"
    mid = len(res_a.equity_curve) // 2
    T = res_a.equity_curve[mid][0]

    def poison(sym: str, upto, t: datetime):
        # bars_view receives already-sliced upto; return as-is (engine slices first).
        return upto

    # Build poisoned full series: after T, destroy closes
    poisoned = {}
    for sym, bars in series.items():
        new_bars = []
        for b in bars:
            if b.ts <= T:
                new_bars.append(b)
            else:
                new_bars.append(Bar(
                    ts=b.ts, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
                ))
        poisoned[sym] = new_bars

    res_b = run_shares_backtest(
        poisoned, cfg=replace_label(cfg, "lookahead_b"),
    )

    # Compare equity curve points with ts <= T
    eq_a = [(ts, round(eq, 6)) for ts, eq in res_a.equity_curve if ts <= T]
    eq_b = [(ts, round(eq, 6)) for ts, eq in res_b.equity_curve if ts <= T]
    assert eq_a == eq_b, (
        f"lookahead detected: equity curves diverge before T={T}\n"
        f"a={eq_a[-3:]}\nb={eq_b[-3:]}"
    )

    tr_a = [t for t in res_a.trades if t.open_date <= T.date().isoformat()]
    tr_b = [t for t in res_b.trades if t.open_date <= T.date().isoformat()]
    # Compare core fields
    def key(t):
        return (t.open_date, t.symbol, t.entry, t.score)
    assert [key(t) for t in tr_a] == [key(t) for t in tr_b]


def replace_label(cfg: BacktestConfig, label: str) -> BacktestConfig:
    from dataclasses import replace
    return replace(cfg, label=label)
