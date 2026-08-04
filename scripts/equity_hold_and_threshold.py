#!/usr/bin/env python3
"""TIME_STOP hold variants + equity threshold table with SPY baseline."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LLM_ENABLED", "false")

from app.backtest.data import load_equity_daily  # noqa: E402
from app.backtest.engine import BacktestConfig, buy_and_hold, run_shares_backtest  # noqa: E402
from app.backtest.metrics import write_metrics_csv  # noqa: E402
from app.config import CANONICAL_EQUITY_UNIVERSE  # noqa: E402

RESULTS = ROOT / "results"


def load_series():
    series = {}
    for t in list(CANONICAL_EQUITY_UNIVERSE):
        bars, rep = load_equity_daily(t)
        series[t] = bars
        print(f"  {rep.symbol} n={rep.n_bars} {rep.first.date()}→{rep.last.date()}")
    return series


def main() -> int:
    print("=== DATA ===")
    series = load_series()
    spy = buy_and_hold(series["SPY"], starting_capital=10_000.0, warmup_bars=60)
    print(f"\nSPY buy-hold: ret={spy.metrics.total_return_pct}% "
          f"equity={spy.metrics.ending_equity}")

    base = BacktestConfig(
        starting_capital=10_000.0,
        risk_pct_per_trade=0.08,
        execute_threshold=75.0,
        market_regime_filter=True,
        best_of_n=True,
        reentry_cooldown_min=0,
        stop_pct=0.025,
        target_pct=0.050,
        max_hold_days=10.0,  # ~240h default shares path in engine uses days
        label="base",
    )

    print("\n=== MAX_HOLD variants (shares path, thr=75, stop 2.5% / tgt 5%) ===")
    # engine uses max_hold_days * 24 hours
    hold_days = [2.0, 4.0, 7.0, 48.0]  # 48h, 96h, 168h, ~none (48 days)
    rows = [spy.metrics]
    for d in hold_days:
        cfg = replace(base, max_hold_days=d, label=f"hold_{d:.0f}d")
        r = run_shares_backtest(series, cfg=cfg)
        m = r.metrics
        ts = sum(1 for t in r.trades if t.reason == "TIME_STOP")
        ts_win = sum(1 for t in r.trades if t.reason == "TIME_STOP" and t.pnl > 0)
        print(
            f"  hold={d:.0f}d (~{d*24:.0f}h): n={m.n_trades} WR={m.win_rate}% "
            f"PF={m.profit_factor} ret={m.total_return_pct}% dd={m.max_drawdown_pct}% "
            f"TIME_STOP={ts} (profitable {ts_win}/{ts}) vsSPY={m.total_return_pct - spy.metrics.total_return_pct:+.2f}pp"
        )
        rows.append(m)

    print("\n=== TIME_STOP profitability detail (hold=10d baseline) ===")
    cfg = replace(base, max_hold_days=10.0, label="hold_10d")
    r = run_shares_backtest(series, cfg=cfg)
    ts_trades = [t for t in r.trades if t.reason == "TIME_STOP"]
    if ts_trades:
        win = [t for t in ts_trades if t.pnl > 0]
        print(f"  TIME_STOP n={len(ts_trades)}  in_profit={len(win)} "
              f"({100*len(win)/len(ts_trades):.1f}%)  "
              f"sum_pnl={sum(t.pnl for t in ts_trades):+.2f}  "
              f"avg={sum(t.pnl for t in ts_trades)/len(ts_trades):+.2f}")
    else:
        print("  no TIME_STOP exits")

    print("\n=== Threshold table (hold=10d, after liquidity scoring fix in process) ===")
    for thr in (65.0, 70.0, 75.0, 80.0):
        cfg = replace(base, execute_threshold=thr, max_hold_days=10.0,
                      label=f"thr_{thr:.0f}")
        r = run_shares_backtest(series, cfg=cfg)
        m = r.metrics
        print(
            f"  thr={thr:.0f}: n={m.n_trades} WR={m.win_rate}% PF={m.profit_factor} "
            f"ret={m.total_return_pct}% dd={m.max_drawdown_pct}% "
            f"vsSPY={m.total_return_pct - spy.metrics.total_return_pct:+.2f}pp"
        )
        rows.append(m)

    RESULTS.mkdir(parents=True, exist_ok=True)
    write_metrics_csv(RESULTS / "equity_hold_threshold.csv", rows)
    print(f"\nWrote {RESULTS / 'equity_hold_threshold.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
