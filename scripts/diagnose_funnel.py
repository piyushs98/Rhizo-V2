#!/usr/bin/env python3
"""
Funnel diagnosis: where symbol-days die before opening.

No strategy changes. Walks the same no-lookahead path as the shares backtest
and counts eliminations stage by stage.

    python scripts/diagnose_funnel.py
    python scripts/diagnose_funnel.py --threshold 62 --run-bt
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LLM_ENABLED", "false")
os.environ.setdefault("DISCORD_WEBHOOK", "")

from app.backtest.data import load_equity_daily, bars_upto  # noqa: E402
from app.backtest.engine import BacktestConfig, run_shares_backtest, buy_and_hold  # noqa: E402
from app.backtest.metrics import write_metrics_csv, write_trades_csv  # noqa: E402
from app.config import CANONICAL_EQUITY_UNIVERSE  # noqa: E402
from app.data.providers import Bar  # noqa: E402
from app.domain.models import Direction, Market, Position  # noqa: E402
from app.engine import indicators as ind, regime as mkt_regime, scoring  # noqa: E402
from app.engine import risk  # noqa: E402
from app.backtest.engine import _patch_settings, _restore_settings  # noqa: E402
from app.db import connection as db_connection  # noqa: E402
from app.db.connection import execute, utcnow  # noqa: E402
from app.db import repositories as repo  # noqa: E402

RESULTS = ROOT / "results"


def pctile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def dist(name: str, xs: list[float]) -> None:
    if not xs:
        print(f"  {name}: NO DATA")
        return
    print(
        f"  {name}: n={len(xs)}  min={min(xs):.2f}  p25={pctile(xs,25):.2f}  "
        f"med={median(xs):.2f}  p75={pctile(xs,75):.2f}  p90={pctile(xs,90):.2f}  "
        f"p95={pctile(xs,95):.2f}  p99={pctile(xs,99):.2f}  max={max(xs):.2f}"
    )


def load_series() -> dict[str, list[Bar]]:
    series = {}
    tickers = list(CANONICAL_EQUITY_UNIVERSE)
    if "SPY" not in tickers:
        tickers.append("SPY")
    for t in tickers:
        bars, rep = load_equity_daily(t, years=2.0, force=False)
        series[t] = bars
        print(f"  {rep.symbol:6s} n={rep.n_bars}  {rep.first.date()}→{rep.last.date()}  src={rep.source}")
    return series


def score_day(
    symbol: str,
    bars: list[Bar],
    spy_closes: list[float],
) -> tuple[str | None, float, dict]:
    """
    Return (elimination_stage_or_None_if_candidate, total, detail).
    Stages before risk: short_history, bearish_long_only, (score always computed
    for bullish), then regime, then threshold applied by caller.
    """
    if len(bars) < 25:
        return "short_history", 0.0, {}
    closes = [b.close for b in bars]
    bullish = ind.trend_score(closes) >= 0
    if not bullish:
        # still score for distribution on long-only book? user wants score dist
        # of evaluated days — score bearish too for calibration, tag separately
        liq = scoring.score_spot_liquidity(bars)
        tech = scoring.score_technical(bars, bullish=False)
        change_pct = 0.0
        if len(closes) >= 2 and closes[-2]:
            change_pct = (closes[-1] / closes[-2] - 1.0) * 100.0
        spy_chg = None
        if len(spy_closes) >= 2 and spy_closes[-2]:
            spy_chg = (spy_closes[-1] / spy_closes[-2] - 1.0) * 100.0
        sent = scoring.score_sentiment(
            change_pct=change_pct, benchmark_change_pct=spy_chg,
            momentum_pct=ind.momentum_pct(closes, 10),
            volume_ratio=ind.volume_ratio(bars, 20),
            bullish=False, news_bias=0.0,
        )
        card = scoring.compose(symbol, liq, tech, sent)
        return "bearish_long_only", card.total, {
            "liq": liq[0], "tech": tech[0], "sent": sent[0],
            "total": card.total, "bullish": False, "inputs": card.inputs,
            "price": closes[-1],
        }

    liq = scoring.score_spot_liquidity(bars)
    tech = scoring.score_technical(bars, bullish=True)
    change_pct = 0.0
    if len(closes) >= 2 and closes[-2]:
        change_pct = (closes[-1] / closes[-2] - 1.0) * 100.0
    spy_chg = None
    if len(spy_closes) >= 2 and spy_closes[-2]:
        spy_chg = (spy_closes[-1] / spy_closes[-2] - 1.0) * 100.0
    sent = scoring.score_sentiment(
        change_pct=change_pct, benchmark_change_pct=spy_chg,
        momentum_pct=ind.momentum_pct(closes, 10),
        volume_ratio=ind.volume_ratio(bars, 20),
        bullish=True, news_bias=0.0,
    )
    card = scoring.compose(symbol, liq, tech, sent)
    detail = {
        "liq": liq[0], "tech": tech[0], "sent": sent[0],
        "total": card.total, "bullish": True, "inputs": card.inputs,
        "price": closes[-1], "liq_detail": liq[1], "tech_detail": tech[1],
        "sent_detail": sent[1],
    }
    return None, card.total, detail


def run_funnel(series: dict[str, list[Bar]], *, threshold: float,
               regime_filter: bool = True, warmup: int = 60) -> dict:
    symbols = [s for s in series if s != "SPY"]
    calendar = [b.ts for b in series["SPY"]]

    # Patch settings so risk gates see $10k book
    cfg = BacktestConfig(
        starting_capital=10_000.0,
        risk_pct_per_trade=0.08,
        execute_threshold=threshold,
        market_regime_filter=regime_filter,
        reentry_cooldown_min=0,
        warmup_bars=warmup,
        label="funnel",
    )
    _patch_settings(cfg)
    try:
        db_connection.init_db()
        execute(
            "UPDATE ledger SET starting_capital=?, cash=?, peak_equity=?, updated_at=? WHERE id=1",
            (cfg.starting_capital, cfg.starting_capital, cfg.starting_capital, utcnow()),
        )

        counts: Counter[str] = Counter()
        # scores among bullish long candidates (pre-threshold)
        scores_all: list[float] = []
        scores_bullish: list[float] = []
        scores_below: list[float] = []
        liqs, techs, sents = [], [], []
        # subcomponents (from bullish days only)
        sub: dict[str, list[float]] = defaultdict(list)
        risk_gates: Counter[str] = Counter()
        opened = 0
        # for threshold sweep: count bullish+regime-ok days by score
        bullish_regime_ok_scores: list[float] = []

        for i, t in enumerate(calendar):
            if i < warmup:
                continue
            spy_v = bars_upto(series["SPY"], t)
            spy_closes = [b.close for b in spy_v]

            # daily entry attempt tracking for max_new_positions etc. uses real risk
            day_opens = 0
            candidates: list[tuple[float, str, dict]] = []

            for sym in symbols:
                counts["total_symbol_days"] += 1
                vb = bars_upto(series[sym], t)
                stage, total, detail = score_day(sym, vb, spy_closes)

                if stage == "short_history":
                    counts["elim_short_history"] += 1
                    continue

                # chain-skip is options-only; not applicable to shares path
                if stage == "bearish_long_only":
                    counts["elim_bearish_long_only"] += 1
                    scores_all.append(total)
                    continue

                # bullish scored
                scores_all.append(total)
                scores_bullish.append(total)
                liqs.append(detail["liq"])
                techs.append(detail["tech"])
                sents.append(detail["sent"])
                for k, v in (detail.get("inputs") or {}).items():
                    if isinstance(v, (int, float)):
                        sub[k].append(float(v))

                if regime_filter and spy_closes:
                    reg = mkt_regime.classify_spy_regime(spy_closes)
                    block = mkt_regime.blocks_direction(reg, Direction.LONG_SHARE.value)
                    if block:
                        counts["elim_regime"] += 1
                        continue

                bullish_regime_ok_scores.append(total)

                if total < threshold:
                    counts["elim_score_below_threshold"] += 1
                    scores_below.append(total)
                    continue

                counts["cleared_score_and_regime"] += 1
                candidates.append((total, sym, detail))

            # best-of-N order
            candidates.sort(key=lambda x: x[0], reverse=True)
            for total, sym, detail in candidates:
                price = float(detail["price"])
                skey = f"EQ-{t.date().isoformat()}"
                key = Position.make_idempotency_key(
                    Market.EQUITY_SHARE, sym, Direction.LONG_SHARE, skey
                )
                decision = risk.check(
                    market=Market.EQUITY_SHARE,
                    underlying=sym,
                    direction=Direction.LONG_SHARE,
                    idempotency_key=key,
                    entry_price=price,
                    multiplier=1.0,
                    whole_units=False,
                    now=t,
                )
                if not decision.allowed:
                    gate = decision.gate or "UNKNOWN_GATE"
                    counts[f"elim_risk_{gate}"] += 1
                    risk_gates[gate] += 1
                    continue
                qty = risk.size_position(
                    decision.max_notional, price, 1.0, whole_units=False
                )
                if qty <= 0:
                    counts["elim_risk_SIZE_ZERO"] += 1
                    risk_gates["SIZE_ZERO"] += 1
                    continue

                # Open a real paper position so subsequent gates (max open, cash) work
                from app.domain.models import ExitPlan, OrderIntent
                from app.broker.paper import PaperBroker
                plan = ExitPlan.build(
                    price, stop_pct=0.025, target_pct=0.05,
                    trail_activate_pct=0.02, trail_giveback_pct=0.35,
                    max_hold_hours=240.0, now=t,
                )
                intent = OrderIntent(
                    market=Market.EQUITY_SHARE, underlying=sym, instrument=sym,
                    direction=Direction.LONG_SHARE, quantity=qty, multiplier=1.0,
                    limit_price=price, session_key=skey, scan_id=f"funnel-{i}",
                    score=total, plan=plan,
                )
                broker = PaperBroker()
                fill = broker.buy(intent)
                pos, created = repo.positions.open_position(intent, fill.price, at=t)
                if created:
                    opened += 1
                    counts["opened"] += 1
                    day_opens += 1
                else:
                    counts["elim_risk_DUPLICATE"] += 1

            # Manage with sim-time exits so cooldown / daily caps see the clock.
            for pos in list(repo.positions.open_positions()):
                vb = bars_upto(series[pos.underlying], t)
                if not vb:
                    continue
                mark = vb[-1].close
                repo.positions.mark(pos.position_id, mark)
                pos2 = repo.positions.get(pos.position_id)
                if not pos2 or not pos2.plan:
                    continue
                from app.engine import exit_rules
                from app.broker.paper import PaperBroker
                sig = exit_rules.evaluate(pos2, mark, now=t)
                if sig.should_close and sig.reason:
                    broker = PaperBroker()
                    fill = broker.sell(pos2, mark, sig.reason.value)
                    repo.positions.close(
                        pos2.position_id, fill.price, sig.reason.value, at=t,
                    )

        # flatten remainder at last sim timestamp
        t_end = calendar[-1]
        for pos in list(repo.positions.open_positions()):
            repo.positions.close(
                pos.position_id,
                pos.mark_price or pos.entry_price or 0.0,
                "END",
                at=t_end,
            )

        return {
            "counts": counts,
            "risk_gates": risk_gates,
            "scores_all": scores_all,
            "scores_bullish": scores_bullish,
            "scores_below": scores_below,
            "bullish_regime_ok_scores": bullish_regime_ok_scores,
            "liqs": liqs,
            "techs": techs,
            "sents": sents,
            "sub": dict(sub),
            "opened": opened,
            "threshold": threshold,
            "n_days": max(0, len(calendar) - warmup),
            "n_symbols": len(symbols),
        }
    finally:
        _restore_settings()


def threshold_for_trades(scores: list[float], target_opens_approx: int,
                         *, pass_rate_to_open: float = 0.35) -> float | None:
    """
    Rough inverse: if every bullish+regime-ok day with score>=thr became a
    candidate, and ~pass_rate_to_open of candidates fill (rest lose to max-open
    etc.), pick thr so candidates * pass_rate ≈ target.

    Better: count how many symbol-days have score >= thr; that is upper bound
    on trades. Report thr where count(score>=thr) equals target (as max trades).
    """
    if not scores:
        return None
    s = sorted(scores, reverse=True)
    if target_opens_approx > len(s):
        return s[-1]
    # threshold just low enough that the top `target` scores clear
    return s[target_opens_approx - 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=75.0)
    ap.add_argument("--run-bt", action="store_true",
                    help="After diagnosis, run full backtest at calibrated thr")
    ap.add_argument("--bt-threshold", type=float, default=None)
    args = ap.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)

    print("=== DATA ===")
    series = load_series()

    print("\n=== NOTE: chain-skip ===")
    print("  Shares path has no option chain. elim_chain_skip = N/A (0).")
    print("  Primary elimination before score is bearish_long_only + regime.")

    print(f"\n=== 1. FUNNEL @ threshold={args.threshold} ===")
    fun = run_funnel(series, threshold=args.threshold, regime_filter=True)
    c = fun["counts"]
    total = c["total_symbol_days"]
    print(f"  trading days (post-warmup): {fun['n_days']}")
    print(f"  symbols (ex-SPY): {fun['n_symbols']}")
    print(f"  total symbol-days evaluated: {total}")

    stages = [
        ("elim_short_history", "short history (<25 bars)"),
        ("elim_bearish_long_only", "bearish (share mode long-only)"),
        ("elim_regime", "MARKET_REGIME (risk-off blocks LONG_SHARE)"),
        ("elim_score_below_threshold", f"score < {args.threshold}"),
        ("cleared_score_and_regime", "cleared score+regime (candidates)"),
        ("opened", "actually opened"),
    ]
    print("\n  Stage counts:")
    for key, label in stages:
        n = c.get(key, 0)
        pct = 100.0 * n / total if total else 0.0
        print(f"    {n:6d}  ({pct:5.1f}%)  {label}")

    print("\n  Risk-gate eliminations (among candidates that cleared score+regime):")
    risk_total = sum(fun["risk_gates"].values())
    if not fun["risk_gates"]:
        print("    (none — or no candidates reached risk)")
    else:
        for gate, n in fun["risk_gates"].most_common():
            print(f"    {n:6d}  {gate}")
        print(f"    total risk eliminations: {risk_total}")

    # residual
    cand = c.get("cleared_score_and_regime", 0)
    print(f"\n  opened={fun['opened']}  candidates={cand}  "
          f"candidate→open rate={100*fun['opened']/cand if cand else 0:.1f}%")

    # biggest stage
    elim_map = {
        "bearish_long_only": c.get("elim_bearish_long_only", 0),
        "regime": c.get("elim_regime", 0),
        "score_below_threshold": c.get("elim_score_below_threshold", 0),
        "risk_gates_combined": risk_total,
        "short_history": c.get("elim_short_history", 0),
    }
    biggest = max(elim_map, key=elim_map.get)
    print(f"\n  >>> LARGEST single elimination bucket: {biggest} = {elim_map[biggest]} "
          f"({100*elim_map[biggest]/total:.1f}% of symbol-days)")

    print("\n=== 2. SCORE DISTRIBUTION (equities) ===")
    print("  -- all scored symbol-days (includes bearish, scored for calibration) --")
    dist("TOTAL all", fun["scores_all"])
    print("  -- bullish only (eligible for long share) --")
    dist("TOTAL bullish", fun["scores_bullish"])
    print("  -- bullish + regime OK (would face threshold only) --")
    dist("TOTAL bull+regime", fun["bullish_regime_ok_scores"])
    print(f"  -- among those below threshold {args.threshold} --")
    dist("below thr", fun["scores_below"])

    # gap analysis
    if fun["bullish_regime_ok_scores"]:
        med = median(fun["bullish_regime_ok_scores"])
        print(f"\n  median bullish+regime score: {med:.2f}")
        print(f"  gap vs threshold {args.threshold}: {args.threshold - med:+.2f} points")
        clear = sum(1 for x in fun["bullish_regime_ok_scores"] if x >= args.threshold)
        print(f"  clear rate @ {args.threshold}: {clear}/{len(fun['bullish_regime_ok_scores'])} "
              f"= {100*clear/len(fun['bullish_regime_ok_scores']):.2f}%")

    print("\n  Threshold → count of bullish+regime symbol-days with score >= thr")
    print("  (upper bound on trades if risk never blocked and 1 open per symbol-day)")
    br = fun["bullish_regime_ok_scores"]
    for thr in range(40, 86, 2):
        n = sum(1 for x in br if x >= thr)
        print(f"    thr={thr:5.1f}  clear_days={n:5d}")

    print("\n  Approx thr for target clear-days (upper bound on trade count):")
    for target in (100, 150, 200, 400):
        thr = threshold_for_trades(br, target)
        if thr is None:
            print(f"    {target} trades: insufficient data")
        else:
            actual = sum(1 for x in br if x >= thr)
            print(f"    ~{target} clear-days: thr≈{thr:.2f}  (exact count @ that thr: {actual})")

    print("\n=== 3. PILLAR DISTRIBUTIONS (bullish days only) ===")
    dist("liquidity", fun["liqs"])
    dist("technical", fun["techs"])
    dist("sentiment", fun["sents"])

    print("\n  Sub-components (bullish days) — flag if p95 << 100:")
    for k in sorted(fun["sub"].keys()):
        xs = fun["sub"][k]
        # only score-like 0-100 keys
        if not k.startswith(("liq.s_", "tech.s_", "sent.s_")) and k not in (
            "liq.s_spread", "liq.s_volume", "liq.s_turnover",
            "tech.s_trend", "tech.s_rsi", "tech.s_range", "tech.s_vol",
            "sent.s_relative", "sent.s_momentum", "sent.s_confirmation",
        ):
            # still show s_* style
            if ".s_" not in k and not k.startswith("s_"):
                continue
        p95 = pctile(xs, 95)
        flag = "  *** DEPRESSED" if p95 < 85 else ""
        print(
            f"    {k:28s}  med={median(xs):7.2f}  p95={p95:7.2f}  max={max(xs):7.2f}{flag}"
        )

    # hardcoded-ish checks
    print("\n  Spot-liquidity defaults (from score_spot_liquidity source):")
    print("    when spread_pct is None: s_spread hardcoded to 85.0 (not 100)")
    print("    turnover graded best=5000 — equity daily volume is shares, often >> 5000")
    print("    so s_turnover may saturate at 100 while s_spread never exceeds 85 without a quote spread")

    # write funnel CSV
    out = RESULTS / "funnel_counts.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["stage", "count"])
        for k, v in c.most_common():
            w.writerow([k, v])
    print(f"\n  Wrote {out}")

    # With sim-time cooldowns fixed, thr~70 should yield hundreds of clear-days
    # and 150+ fills after max-open / daily-cap. Prefer thr that yields ~200+
    # clear-days; production thr=75 is also re-run for comparison.
    if br:
        # thr for ~250 clear-days → room for risk gates to leave 150+ opens
        recommended = threshold_for_trades(br, 250) or 70.0
    else:
        recommended = 70.0

    bt_thr = args.bt_threshold if args.bt_threshold is not None else recommended
    print(f"\n=== 4. RECOMMENDED BT THRESHOLD ===")
    print(f"  calibrated thr (~250 clear-days upper bound): {recommended:.2f}")
    print(f"  will run backtest at: {bt_thr:.2f}")
    print(f"  also re-run production thr=75 for comparison after sim-time fix")

    spy = buy_and_hold(series["SPY"], starting_capital=10_000.0, warmup_bars=60,
                       label="buy_hold_SPY")
    print(f"\n  SPY buy-hold: return {spy.metrics.total_return_pct}%  "
          f"equity {spy.metrics.ending_equity:,.2f}")

    metrics_out = [spy.metrics]
    for thr in sorted({bt_thr, 75.0, 70.0, 65.0}):
        print(f"\n=== BACKTEST @ thr={thr:.2f} ===")
        cfg = BacktestConfig(
            starting_capital=10_000.0,
            risk_pct_per_trade=0.08,
            execute_threshold=thr,
            market_regime_filter=True,
            best_of_n=True,
            reentry_cooldown_min=0,
            stop_pct=0.025,
            target_pct=0.050,
            label=f"shares_thr_{thr:.1f}_simtime",
        )
        bt = run_shares_backtest(series, cfg=cfg)
        m = bt.metrics
        print(f"  capital {m.starting_capital:,.2f} → {m.ending_equity:,.2f}")
        print(f"  total return {m.total_return_pct}%  ann {m.annualized_return_pct}%")
        print(f"  trades {m.n_trades}  win_rate {m.win_rate}%  "
              f"avg_win {m.avg_win} avg_loss {m.avg_loss}")
        print(f"  largest_win {m.largest_win}  largest_loss {m.largest_loss}")
        print(f"  profit_factor {m.profit_factor}  max_dd {m.max_drawdown_pct}% "
              f"({m.max_dd_duration_days}d)")
        print(f"  trades/month {m.trades_per_month}  lose_streak {m.longest_losing_streak}")
        print(f"  exits {m.exits_by_reason}")
        print(f"  return w/o best trade {m.return_without_best_trade_pct}% "
              f"(best {m.best_trade_pnl})")
        if m.notes:
            print(f"  NOTES: {m.notes}")
        print(f"  vs SPY: {m.total_return_pct - spy.metrics.total_return_pct:+.2f}pp")
        if m.n_trades >= 150 and m.total_return_pct < 0:
            print("  VERDICT: measurable losing strategy (n>=150, negative return).")
        elif m.n_trades < 50:
            print("  VERDICT: still too few trades.")
        write_trades_csv(RESULTS / f"funnel_bt_thr{thr:.1f}_trades.csv", bt.trades)
        metrics_out.append(m)

    write_metrics_csv(RESULTS / "funnel_bt_metrics.csv", metrics_out)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
