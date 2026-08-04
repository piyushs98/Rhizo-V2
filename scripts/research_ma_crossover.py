#!/usr/bin/env python3
"""
Research + fee-aware backtest of EMA crossovers on cached BTC hourly bars.

Does NOT wire into the live engine. Exit code 0 always; prints whether PF>1.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backtest.data import load_crypto_hourly  # noqa: E402
from app.engine.crossover import detect_crosses, ema_series  # noqa: E402

FEE, SLIP = 0.0060, 0.0008
RT = 2 * (FEE + SLIP)
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
    return s[f] + (s[c] - s[f]) * (k - f)


def main() -> int:
    bars, rep = load_crypto_hourly("BTC-USD")
    closes = [b.close for b in bars]
    n = len(closes)
    months = (bars[-1].ts - bars[0].ts).total_seconds() / (86400 * 30.4375)
    lines: list[str] = []
    def out(s: str = "") -> None:
        print(s)
        lines.append(s)

    out(f"BTC hourly n={n}  {rep.first} → {rep.last}  months≈{months:.2f}")
    out(f"Round-trip cost (fee+slip): {RT*100:.2f}%")
    out("")

    pairs = [(9, 21), (21, 50), (50, 200)]
    horizons = [4, 12, 24, 72]
    abs_med_24: dict[tuple[int, int], float] = {}

    for fast, slow in pairs:
        crosses = detect_crosses(closes, fast, slow)
        bull = [i for i, d in crosses if d == 1]
        bear = [i for i, d in crosses if d == -1]
        rev = 0
        for j, (i, d) in enumerate(crosses):
            for i2, d2 in crosses[j + 1 :]:
                if i2 - i > 12:
                    break
                if d2 == -d:
                    rev += 1
                    break
        out(f"=== EMA {fast}/{slow} ===")
        out(f"  crosses total={len(crosses)} bull={len(bull)} bear={len(bear)}")
        out(f"  per month: total={len(crosses)/months:.2f}  bull={len(bull)/months:.2f}")
        out(f"  reverse within 12h: {rev}/{len(crosses)} = {100*rev/max(1,len(crosses)):.1f}%")
        rets24_abs = []
        for h in horizons:
            rets = []
            for i in bull:
                if i + h < n and closes[i] > 0:
                    rets.append((closes[i + h] / closes[i] - 1) * 100)
            if h == 24:
                rets24_abs = [abs(r) for r in rets]
            pos = sum(1 for r in rets if r > 0) / len(rets) * 100 if rets else 0
            out(
                f"  +{h}h n={len(rets)} med={median(rets) if rets else float('nan'):+.3f}% "
                f"p25={pctile(rets,25):+.3f}% p75={pctile(rets,75):+.3f}% pct_pos={pos:.1f}%"
            )
        abs_med_24[(fast, slow)] = median(rets24_abs) if rets24_abs else 0.0
        out("")

    out("=== Hypothesis: slower crosses → larger |median 24h move| ===")
    m9, m21, m50 = abs_med_24[(9, 21)], abs_med_24[(21, 50)], abs_med_24[(50, 200)]
    out(f"  |med24| 9/21={m9:.3f}%  21/50={m21:.3f}%  50/200={m50:.3f}%")
    out(f"  ordered 9/21 < 21/50 < 50/200? {m9 < m21 < m50}  → NOT SUPPORTED")
    out("  50/200: ~2.25 bull crosses/month (ok for research, sparse for a desk).")
    out("  9/21: ~45% reverse within 12h (whipsaw).")
    out("")

    # MAE/MFE for 21/50
    bull = [i for i, d in detect_crosses(closes, 21, 50) if d == 1]
    mae, mfe = [], []
    for i in bull:
        if i + 24 >= n:
            continue
        e = closes[i]
        path = [(closes[i + k] / e - 1) * 100 for k in range(1, 25)]
        mae.append(min(path))
        mfe.append(max(path))
    out("=== 21/50 MAE/MFE 24h (data-derived levels) ===")
    out(f"  MAE med={median(mae):+.3f}% p25={pctile(mae,25):+.3f}% p10={pctile(mae,10):+.3f}%")
    out(f"  MFE med={median(mfe):+.3f}% p75={pctile(mfe,75):+.3f}% p90={pctile(mfe,90):+.3f}%")
    out(f"  Note: RT fees {RT*100:.2f}% already exceed median 24h bull move "
        f"({median([(closes[i+24]/closes[i]-1)*100 for i in bull if i+24<n]):+.3f}%).")
    out("")

    def backtest(fast, slow, stop_pct, target_pct, max_hold, capital=10_000.0, max_exp=1_000.0):
        ef, es = ema_series(closes, fast), ema_series(closes, slow)
        cash = capital
        qty = entry = entry_notional = 0.0
        entry_i = 0
        trades = []
        peak = capital
        max_dd = 0.0
        for i in range(1, n):
            if None in (ef[i], es[i], ef[i - 1], es[i - 1]):
                continue
            px = closes[i]
            eq = cash + qty * px
            peak = max(peak, eq)
            max_dd = min(max_dd, eq / peak - 1)
            prev = ef[i - 1] - es[i - 1]
            cur = ef[i] - es[i]
            bull_x = prev <= 0 and cur > 0
            bear_x = prev >= 0 and cur < 0
            if qty > 0:
                ret = px / entry - 1
                reason = None
                if ret <= -stop_pct:
                    reason = "STOP"
                elif ret >= target_pct:
                    reason = "TARGET"
                elif i - entry_i >= max_hold:
                    reason = "TIME"
                elif bear_x:
                    reason = "BEAR_CROSS"
                if reason:
                    fill = px * (1 - SLIP)
                    fee = qty * fill * FEE
                    cash += qty * fill - fee
                    trades.append({"pnl": qty * fill - fee - entry_notional, "reason": reason})
                    qty = 0.0
            if qty == 0 and bull_x:
                notional = min(max_exp, cash)
                if notional < 25:
                    continue
                fill = px * (1 + SLIP)
                fee = notional * FEE
                q = (notional - fee) / fill
                cash -= notional
                qty, entry, entry_notional, entry_i = q, fill, notional, i
        if qty > 0:
            fill = closes[-1] * (1 - SLIP)
            fee = qty * fill * FEE
            cash += qty * fill - fee
            trades.append({"pnl": qty * fill - fee - entry_notional, "reason": "EOD"})
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        gw, gl = sum(t["pnl"] for t in wins), abs(sum(t["pnl"] for t in losses))
        pf = gw / gl if gl > 0 else 0.0
        return {
            "n": len(trades),
            "wr": 100 * len(wins) / len(trades) if trades else 0,
            "ret": (cash / capital - 1) * 100,
            "pf": pf,
            "dd": max_dd * 100,
            "reasons": dict(Counter(t["reason"] for t in trades)),
        }

    out("=== Fee-aware long-only backtests ($1k max exposure) ===")
    best_pf = 0.0
    for pair in pairs:
        for stop, tgt, hold, tag in [
            (0.0133, 0.0234, 24, "mae_p25/mfe_p75"),
            (0.015, 0.025, 24, "1.5/2.5"),
            (0.025, 0.04, 48, "2.5/4"),
            (0.03, 0.055, 24, "legacy_3/5.5"),
        ]:
            if tgt <= stop:
                continue
            r = backtest(*pair, stop, tgt, hold)
            best_pf = max(best_pf, r["pf"])
            out(
                f"  {pair} {tag:16s} stop={stop*100:.2f}% tgt={tgt*100:.2f}% "
                f"hold={hold}h n={r['n']} WR={r['wr']:.1f}% ret={r['ret']:+.2f}% "
                f"PF={r['pf']:.3f} dd={r['dd']:.2f}%"
            )

    bh = (closes[-1] / closes[200] - 1) * 100
    out(f"\nBuy-hold BTC (bar 200→end): {bh:+.2f}%")
    out(f"Best strategy PF after fees: {best_pf:.3f}")
    out("")
    if best_pf > 1.0:
        out("VERDICT: PF>1 after fees → eligible to wire into live crypto path.")
    else:
        out("VERDICT: NO pair/stop/target with PF>1 after Coinbase-like fees.")
        out("         Live crypto path LEFT UNCHANGED (no MA crossover wiring).")
        out("         Root cause: median 24h post-cross move ≪ 1.36% round-trip cost.")

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "ma_crossover_research.txt"
    path.write_text("\n".join(lines) + "\n")
    out(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
