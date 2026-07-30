#!/usr/bin/env python3
"""
Janus Desk walk-forward backtest.

Non-negotiable: at each timestamp T the strategy sees only bars with ts <= T.
Uses production scoring, risk, exit_rules, and PaperBroker.

    python scripts/backtest.py                  # full equity matrix + crypto
    python scripts/backtest.py --equity-only
    python scripts/backtest.py --crypto-only
    python scripts/backtest.py --force-download  # refresh cache

Options path: NOT historically reconstructed (Alpaca options history is short
and incomplete for premium/spread fidelity). Primary path is SHARES.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolate default DB before app imports bind paths for any incidental use.
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LLM_ENABLED", "false")
os.environ.setdefault("DISCORD_WEBHOOK", "")
os.environ.setdefault("INTER_SYMBOL_SLEEP_S", "0")

from app.backtest.data import (  # noqa: E402
    load_crypto_hourly, load_equity_daily,
)
from app.backtest.engine import (  # noqa: E402
    BacktestConfig, buy_and_hold, run_shares_backtest,
)
from app.backtest.crypto_engine import run_crypto_backtest  # noqa: E402
from app.backtest.metrics import (  # noqa: E402
    required_win_rate, write_metrics_csv, write_monthly_csv, write_trades_csv,
)
from app.config import CANONICAL_EQUITY_UNIVERSE  # noqa: E402

RESULTS = ROOT / "results"


def _print_metrics(m, prefix: str = "") -> None:
    print(f"{prefix}{m.label}")
    print(f"{prefix}  capital {m.starting_capital:,.2f} → equity {m.ending_equity:,.2f}")
    print(f"{prefix}  total return {m.total_return_pct}%  ann {m.annualized_return_pct}%")
    print(f"{prefix}  trades {m.n_trades}  win_rate {m.win_rate}%  "
          f"avg_win {m.avg_win} avg_loss {m.avg_loss}")
    print(f"{prefix}  profit_factor {m.profit_factor}  max_dd {m.max_drawdown_pct}% "
          f"({m.max_dd_duration_days}d)  lose_streak {m.longest_losing_streak}")
    print(f"{prefix}  exits {m.exits_by_reason}")
    if m.return_without_best_trade_pct is not None:
        print(f"{prefix}  return w/o best trade {m.return_without_best_trade_pct}% "
              f"(best trade pnl {m.best_trade_pnl})")
    if m.notes:
        print(f"{prefix}  NOTES: {m.notes}")


def load_equity_bundle(force: bool) -> tuple[dict, list]:
    reports = []
    series = {}
    tickers = list(CANONICAL_EQUITY_UNIVERSE)
    if "SPY" not in tickers:
        tickers.append("SPY")
    for t in tickers:
        bars, rep = load_equity_daily(t, years=2.0, force=force)
        series[t] = bars
        reports.append(rep)
        print(f"  {rep.symbol:6s} 1d n={rep.n_bars:4d}  "
              f"{rep.first.date() if rep.first else '?'} → "
              f"{rep.last.date() if rep.last else '?'}  "
              f"gaps={rep.gaps}  src={rep.source}")
    return series, reports


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--equity-only", action="store_true")
    ap.add_argument("--crypto-only", action="store_true")
    ap.add_argument("--force-download", action="store_true")
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--quick", action="store_true",
                    help="Skip variant matrix; run baseline + SPY hold only")
    args = ap.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    all_metrics = []

    print("=" * 72)
    print("OPTIONS PATH: UNTESTED")
    print("  Alpaca free options history starts ~Feb 2024 and does not provide")
    print("  a faithful reconstructed mid/spread for a specific contract over")
    print("  multi-year walks. No synthetic BS premiums used. Primary path = SHARES.")
    print("=" * 72)

    if not args.crypto_only:
        print("\n### EQUITY DATA ###")
        series, reports = load_equity_bundle(args.force_download)
        spy = series["SPY"]
        base_cfg = BacktestConfig(
            starting_capital=args.capital,
            risk_pct_per_trade=0.08,
            execute_threshold=75.0,
            market_regime_filter=True,
            best_of_n=True,
            stop_pct=0.025,
            target_pct=0.050,
            label="shares_baseline_8pct_thr75_regime_bon",
        )

        print("\n### BASELINE: shares strategy ###")
        bt = run_shares_backtest(series, cfg=base_cfg)
        _print_metrics(bt.metrics)
        write_trades_csv(RESULTS / "equity_baseline_trades.csv", bt.trades)
        write_monthly_csv(RESULTS / "equity_baseline_monthly.csv", bt.metrics.months)
        all_metrics.append(bt.metrics)

        print("\n### BASELINE: buy-and-hold SPY (same capital, same window) ###")
        bh = buy_and_hold(spy, starting_capital=args.capital, warmup_bars=base_cfg.warmup_bars,
                          label="buy_hold_SPY")
        _print_metrics(bh.metrics)
        all_metrics.append(bh.metrics)
        print(f"\n  STRATEGY vs SPY: strategy {bt.metrics.total_return_pct}%  "
              f"SPY {bh.metrics.total_return_pct}%  "
              f"delta {bt.metrics.total_return_pct - bh.metrics.total_return_pct:+.2f}pp")
        if bt.metrics.total_return_pct < bh.metrics.total_return_pct:
            print("  VERDICT: strategy underperformed SPY buy-and-hold on this window.")

        if not args.quick:
            print("\n### VARIANTS (one variable at a time) ###")

            # Exit structures
            exit_variants = [
                ("exit_60_35", 0.35, 0.60, None),  # option-style (harsh on shares)
                ("exit_15_35", 0.35, 0.15, None),
                ("exit_15_12", 0.12, 0.15, None),
                ("exit_scale_half_15_trail", 0.025, 0.050, 0.15),
                ("exit_shares_default_2.5_5", 0.025, 0.050, None),
            ]
            print("\n  -- exit structure --")
            for name, stop, target, scale in exit_variants:
                be = required_win_rate(stop, target, fee_rt=0.001)  # ~10bps RT equity
                cfg = replace(
                    base_cfg, label=name, stop_pct=stop, target_pct=target,
                    scale_out_half_at=scale,
                    trail_activate_pct=min(target * 0.5, stop) if target > stop else target * 0.5,
                )
                # invalid if target <= stop for non-scale; still run and report
                try:
                    r = run_shares_backtest(series, cfg=cfg)
                    _print_metrics(r.metrics, prefix="  ")
                    print(f"    required WR (pre-fee approx) for stop={stop} target={target}: "
                          f"{be*100:.1f}%  achieved={r.metrics.win_rate}")
                    all_metrics.append(r.metrics)
                    write_trades_csv(RESULTS / f"{name}_trades.csv", r.trades)
                except Exception as exc:
                    print(f"  {name} FAILED: {exc}")

            print("\n  -- EXECUTE_THRESHOLD --")
            for thr in (70.0, 75.0, 80.0):
                cfg = replace(base_cfg, label=f"thr_{int(thr)}", execute_threshold=thr)
                r = run_shares_backtest(series, cfg=cfg)
                _print_metrics(r.metrics, prefix="  ")
                all_metrics.append(r.metrics)

            print("\n  -- regime filter --")
            for on in (True, False):
                cfg = replace(base_cfg, label=f"regime_{'ON' if on else 'OFF'}",
                              market_regime_filter=on)
                r = run_shares_backtest(series, cfg=cfg)
                _print_metrics(r.metrics, prefix="  ")
                all_metrics.append(r.metrics)

            print("\n  -- RISK_PCT --")
            for rp in (0.05, 0.08, 0.10):
                cfg = replace(base_cfg, label=f"risk_{int(rp*100)}pct",
                              risk_pct_per_trade=rp)
                r = run_shares_backtest(series, cfg=cfg)
                _print_metrics(r.metrics, prefix="  ")
                all_metrics.append(r.metrics)

            print("\n  -- best-of-N --")
            for bon in (True, False):
                cfg = replace(base_cfg, label=f"bon_{'ON' if bon else 'OFF'}",
                              best_of_n=bon)
                r = run_shares_backtest(series, cfg=cfg)
                _print_metrics(r.metrics, prefix="  ")
                all_metrics.append(r.metrics)

            print("\n  -- OTM documentation (shares proxy: no options history) --")
            print("  Options OTM path cannot be backtested without historical premiums.")
            print("  Documented: skipped (not synthesised).")

    if not args.equity_only:
        print("\n### CRYPTO DATA ###")
        bars, rep = load_crypto_hourly("BTC-USD", days=365, force=args.force_download)
        print(f"  {rep.symbol} 1h n={rep.n_bars}  {rep.first} → {rep.last}  "
              f"gaps={rep.gaps} src={rep.source}")

        print("\n### CRYPTO scalp / percentage exits @ $10k book, $1k max exposure ###")
        cx_cfg = BacktestConfig(
            starting_capital=args.capital,
            risk_pct_per_trade=0.08,
            label="crypto_scalp_cap1000",
            warmup_bars=48,
        )
        cx = run_crypto_backtest(
            bars, cfg=cx_cfg, crypto_max_exposure=1000.0,
            execute_threshold=70.0, use_scalp_gate=True,
        )
        _print_metrics(cx.metrics)
        write_trades_csv(RESULTS / "crypto_trades.csv", cx.trades)
        all_metrics.append(cx.metrics)

        be = required_win_rate(0.03, 0.055, fee_rt=0.012)
        print(f"  break-even WR @ 3/5.5 + 1.2% RT fees: {be*100:.1f}%")
        print(f"  achieved WR: {cx.metrics.win_rate}")
        if cx.metrics.win_rate is not None and cx.metrics.win_rate / 100.0 < be:
            print("  VERDICT: achieved WR below fee break-even — fees eat the edge.")
        elif cx.metrics.n_trades < 50:
            print("  VERDICT: too few trades to claim an edge.")

        print("\n### buy-and-hold BTC (same capital, no fee model) ###")
        bh = buy_and_hold(bars, starting_capital=args.capital, warmup_bars=48,
                          label="buy_hold_BTC")
        _print_metrics(bh.metrics)
        all_metrics.append(bh.metrics)
        print(f"  CRYPTO strat {cx.metrics.total_return_pct}% vs HODL "
              f"{bh.metrics.total_return_pct}%")

    write_metrics_csv(RESULTS / "summary_metrics.csv", all_metrics)
    summary_path = RESULTS / "SUMMARY.txt"
    with summary_path.open("w") as f:
        f.write("Janus Desk backtest summary\n")
        f.write("Options path: UNTESTED (no faithful historical premiums).\n")
        f.write("Primary path: equity SHARES walk-forward + crypto spot.\n\n")
        for m in all_metrics:
            f.write(
                f"{m.label}: ret={m.total_return_pct}% n={m.n_trades} "
                f"wr={m.win_rate} dd={m.max_drawdown_pct}% pf={m.profit_factor} "
                f"| {m.notes}\n"
            )
    print(f"\nWrote {RESULTS}/summary_metrics.csv and SUMMARY.txt")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
