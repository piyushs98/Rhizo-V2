#!/usr/bin/env python3
"""
Research: match SPY return with lower drawdown via regime timing.

Strategy (entire book): hold SPY when risk-on, cash when risk-off.
No stock-picking, no pillars, no scoring.

Not wired into the live engine.

    python scripts/research_spy_regime_timing.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backtest.data import load_equity_daily  # noqa: E402
from app.engine.indicators import sma  # noqa: E402

RESULTS = ROOT / "results"

# Paper equity-like costs: ~5 bps slip each side, no commission.
SLIP = 0.0005
FEE = 0.0  # commission-free
RT = 2 * (SLIP + FEE)


@dataclass
class Switch:
    date: str
    action: str  # BUY or SELL
    price: float
    reason: str


@dataclass
class Whipsaw:
    sell_date: str
    sell_price: float
    buy_date: str
    buy_price: float
    giveback_pct: float  # (buy-sell)/sell * 100, positive = rebuy higher (bad)


def regime_series(
    closes: list[float],
    kind: str,
) -> list[str]:
    """
    Per-bar regime labels aligned to closes. 'cash' on warmup UNKNOWN days
    (not yet in market — conservative).
    """
    out: list[str] = []
    n = len(closes)
    for i in range(n):
        window = closes[: i + 1]
        if kind == "sma50_200_cross":
            if len(window) < 200:
                out.append("unknown")
                continue
            m50 = sma(window, 50)
            m200 = sma(window, 200)
            if m50 is None or m200 is None:
                out.append("unknown")
            elif m50 > m200:
                out.append("risk_on")
            else:
                out.append("risk_off")
        elif kind.startswith("sma") and kind[3:].isdigit():
            period = int(kind[3:])
            if len(window) < period:
                out.append("unknown")
                continue
            m = sma(window, period)
            if m is None or m <= 0:
                out.append("unknown")
            elif window[-1] > m:
                out.append("risk_on")
            else:
                out.append("risk_off")
        elif kind == "live_sma20_slope":
            # Current live desk rule (for reference only).
            period, slope_lb = 20, 5
            if len(window) < max(period, slope_lb + 1):
                out.append("unknown")
                continue
            m = sma(window, period)
            base = window[-(slope_lb + 1)]
            if m is None or m <= 0 or base <= 0:
                out.append("unknown")
            elif window[-1] > m and (window[-1] - base) >= 0:
                out.append("risk_on")
            else:
                out.append("risk_off")
        else:
            raise ValueError(kind)
    return out


def max_drawdown(curve: list[float]) -> tuple[float, int]:
    """Return (max_dd_pct, duration_in_bars of the worst underwater stretch)."""
    if not curve:
        return 0.0, 0
    peak = curve[0]
    max_dd = 0.0
    worst_dur = 0
    dd_start = None
    for i, eq in enumerate(curve):
        if eq >= peak:
            peak = eq
            if dd_start is not None:
                worst_dur = max(worst_dur, i - dd_start)
            dd_start = None
        else:
            if dd_start is None:
                dd_start = i
            dd = eq / peak - 1.0
            if dd < max_dd:
                max_dd = dd
    if dd_start is not None:
        worst_dur = max(worst_dur, len(curve) - dd_start)
    return max_dd * 100.0, worst_dur


def backtest_regime(
    dates: list,
    closes: list[float],
    regimes: list[str],
    *,
    capital: float = 10_000.0,
    start_i: int = 200,
) -> dict:
    """
    Trade at close of day i when regime[i] differs from position.
    Decision uses regime known at close (same bar) — standard for daily
    research; note lookahead of open-of-next would be slightly different.
    """
    cash = capital
    shares = 0.0
    in_market = False
    switches: list[Switch] = []
    curve: list[float] = []
    in_mkt_days = 0
    cash_days = 0

    for i in range(start_i, len(closes)):
        px = closes[i]
        reg = regimes[i]
        want_on = reg == "risk_on"

        if want_on and not in_market:
            fill = px * (1 + SLIP)
            fee = 0.0
            shares = (cash - fee) / fill
            cash = 0.0
            in_market = True
            switches.append(Switch(
                date=str(dates[i].date()) if hasattr(dates[i], "date") else str(dates[i]),
                action="BUY", price=fill, reason=reg,
            ))
        elif (not want_on) and in_market:
            fill = px * (1 - SLIP)
            cash = shares * fill
            shares = 0.0
            in_market = False
            switches.append(Switch(
                date=str(dates[i].date()) if hasattr(dates[i], "date") else str(dates[i]),
                action="SELL", price=fill, reason=reg,
            ))

        eq = cash + shares * px
        curve.append(eq)
        if in_market:
            in_mkt_days += 1
        else:
            cash_days += 1

    # flatten at end if still long
    if shares > 0:
        fill = closes[-1] * (1 - SLIP)
        cash = shares * fill
        shares = 0.0
        switches.append(Switch(
            date=str(dates[-1].date()) if hasattr(dates[-1], "date") else str(dates[-1]),
            action="SELL", price=fill, reason="EOD",
        ))
        curve[-1] = cash

    final = cash if shares == 0 else cash + shares * closes[-1]
    total_ret = (final / capital - 1.0) * 100.0
    max_dd, dd_bars = max_drawdown(curve)
    n_days = len(curve)
    # switches that are real round-trips count as fee events: each BUY/SELL pair
    n_switches = len([s for s in switches if s.action in ("BUY", "SELL") and s.reason != "EOD"])
    # round trips approx
    n_buys = sum(1 for s in switches if s.action == "BUY")
    n_sells = sum(1 for s in switches if s.action == "SELL")

    # Whipsaws: SELL then next BUY at higher price
    whips: list[Whipsaw] = []
    last_sell = None
    for s in switches:
        if s.action == "SELL" and s.reason != "EOD":
            last_sell = s
        elif s.action == "BUY" and last_sell is not None:
            gb = (s.price / last_sell.price - 1.0) * 100.0
            if gb > 0:  # rebuy higher = classic whipsaw loss of timing
                whips.append(Whipsaw(
                    sell_date=last_sell.date,
                    sell_price=last_sell.price,
                    buy_date=s.date,
                    buy_price=s.price,
                    giveback_pct=gb,
                ))
            last_sell = None

    return {
        "final": final,
        "total_ret": total_ret,
        "max_dd": max_dd,
        "dd_bars": dd_bars,
        "dd_days": dd_bars,  # daily bars
        "n_switch_events": n_buys + n_sells,
        "n_round_trips": min(n_buys, n_sells),
        "n_buys": n_buys,
        "n_sells": n_sells,
        "in_mkt_days": in_mkt_days,
        "cash_days": cash_days,
        "pct_in_market": 100.0 * in_mkt_days / n_days if n_days else 0,
        "pct_in_cash": 100.0 * cash_days / n_days if n_days else 0,
        "ret_per_dd": (total_ret / abs(max_dd)) if max_dd < 0 else None,
        "switches": switches,
        "whipsaws": whips,
        "curve": curve,
    }


def buy_hold(closes: list[float], *, capital: float = 10_000.0, start_i: int = 200) -> dict:
    entry = closes[start_i] * (1 + SLIP)
    shares = capital / entry
    curve = [shares * closes[i] for i in range(start_i, len(closes))]
    final = shares * closes[-1] * (1 - SLIP)
    total_ret = (final / capital - 1.0) * 100.0
    max_dd, dd_bars = max_drawdown(curve)
    return {
        "final": final,
        "total_ret": total_ret,
        "max_dd": max_dd,
        "dd_bars": dd_bars,
        "ret_per_dd": (total_ret / abs(max_dd)) if max_dd < 0 else None,
        "pct_in_market": 100.0,
        "n_round_trips": 0,
    }


def main() -> int:
    bars, rep = load_equity_daily("SPY")
    closes = [b.close for b in bars]
    dates = [b.ts for b in bars]
    # Align start to warmest definition (SMA200 / 50-200 cross)
    start_i = 200
    print(f"SPY daily n={len(bars)}  {rep.first.date()} → {rep.last.date()}  src={rep.source}")
    print(f"Evaluation from bar {start_i} ({dates[start_i].date()}) to {dates[-1].date()}")
    print(f"Costs: slip {SLIP*10000:.0f} bps/side  RT={RT*100:.2f}% per round trip")
    print()

    bh = buy_hold(closes, start_i=start_i)
    print("=== BUY-AND-HOLD SPY (benchmark) ===")
    print(f"  total return:     {bh['total_ret']:+.2f}%")
    print(f"  max drawdown:     {bh['max_dd']:.2f}%  ({bh['dd_bars']} trading days underwater peak→trough stretch)")
    print(f"  ret / |dd|:       {bh['ret_per_dd']:.3f}" if bh["ret_per_dd"] else "  ret/|dd|: n/a")
    print(f"  time in market:   100%")
    print()

    # Success criteria
    target_ret_floor = bh["total_ret"] - 5.0  # within ~5pp
    target_dd = bh["max_dd"] / 2.0            # half the drawdown (less negative)
    print(f"SUCCESS if return ≥ {target_ret_floor:+.1f}% (within ~5pp of HODL) "
          f"AND max DD ≥ {target_dd:.1f}% (half of HODL DD in magnitude).")
    print()

    variants = [
        ("sma20", "Price > SMA20"),
        ("sma50", "Price > SMA50"),
        ("sma200", "Price > SMA200"),
        ("sma50_200_cross", "SMA50 > SMA200"),
        ("live_sma20_slope", "Live desk: SMA20 + non-neg 5d slope (reference)"),
    ]

    results = {}
    for kind, label in variants:
        regimes = regime_series(closes, kind)
        r = backtest_regime(dates, closes, regimes, start_i=start_i)
        results[kind] = r
        ok_ret = r["total_ret"] >= target_ret_floor
        ok_dd = r["max_dd"] >= target_dd  # e.g. -9 > -19
        success = ok_ret and ok_dd
        print(f"=== {kind}: {label} ===")
        print(f"  total return:     {r['total_ret']:+.2f}%   (HODL {bh['total_ret']:+.2f}%, delta {r['total_ret']-bh['total_ret']:+.2f}pp)")
        print(f"  max drawdown:     {r['max_dd']:.2f}%   (HODL {bh['max_dd']:.2f}%)  dur≈{r['dd_days']} bars")
        print(f"  ret / |dd|:       {r['ret_per_dd']:.3f}" if r["ret_per_dd"] is not None else "  ret/|dd|: n/a",
              f"  (HODL {bh['ret_per_dd']:.3f})" if bh["ret_per_dd"] else "")
        print(f"  regime switches:  {r['n_switch_events']} events  ({r['n_round_trips']} round-trips, each pays ~{RT*100:.2f}% RT)")
        print(f"  time in market:   {r['pct_in_market']:.1f}%   cash: {r['pct_in_cash']:.1f}%")
        print(f"  whipsaws (rebuy higher): {len(r['whipsaws'])}")
        print(f"  SUCCESS criteria: {success}  (ret_ok={ok_ret}, dd_ok={ok_dd})")
        print()

    # Whipsaw detail for best ret/dd candidate and for sma20
    print("=== WHIPSAW FAILURE MODE (rebuy higher after selling) ===")
    for kind, _ in variants:
        whips = results[kind]["whipsaws"]
        if not whips:
            print(f"  {kind}: no rebuy-higher whipsaws")
            continue
        whips_sorted = sorted(whips, key=lambda w: -w.giveback_pct)
        print(f"  {kind}: n={len(whips)}  worst givebacks:")
        for w in whips_sorted[:5]:
            print(
                f"    sold {w.sell_date} @ {w.sell_price:.2f} → "
                f"rebought {w.buy_date} @ {w.buy_price:.2f}  "
                f"(+{w.giveback_pct:.2f}% higher)"
            )
        avg = sum(w.giveback_pct for w in whips) / len(whips)
        print(f"    mean giveback when whipsaw: +{avg:.2f}%")
        print()

    # Plain-language verdict
    print("=== VERDICT ===")
    winners = []
    for kind, label in variants:
        r = results[kind]
        if r["total_ret"] >= target_ret_floor and r["max_dd"] >= target_dd:
            winners.append(kind)
    if winners:
        print(f"  SUCCESS: {', '.join(winners)} meet return-near-HODL + half-DD.")
    else:
        print("  NONE of the regime timers achieve “near SPY return with ~half drawdown.”")
        # Did any improve ret/dd?
        improved = []
        for kind, _ in variants:
            r = results[kind]
            if r["ret_per_dd"] is not None and bh["ret_per_dd"] is not None:
                if r["ret_per_dd"] > bh["ret_per_dd"] and r["max_dd"] > bh["max_dd"]:
                    improved.append(
                        f"{kind} (ret {r['total_ret']:+.1f}% dd {r['max_dd']:.1f}% "
                        f"ret/|dd| {r['ret_per_dd']:.2f} vs HODL {bh['ret_per_dd']:.2f})"
                    )
        if improved:
            print("  Some cut DD and improved ret/|dd|, but returned too little:")
            for s in improved:
                print(f"    - {s}")
        print("  LEGITIMATE FINDING: buy-and-hold SPY is better for total wealth on this window;")
        print("  regime-to-cash does not cleanly deliver “same return, half the pain.”")

    # Write report
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "spy_regime_timing.txt"
    # re-print is already on stdout; dump a compact CSV too
    csv_path = RESULTS / "spy_regime_timing.csv"
    with csv_path.open("w") as f:
        f.write("strategy,total_ret_pct,max_dd_pct,dd_bars,ret_per_abs_dd,round_trips,pct_in_market,n_whipsaws\n")
        f.write(
            f"buy_hold,{bh['total_ret']:.4f},{bh['max_dd']:.4f},{bh['dd_bars']},"
            f"{bh['ret_per_dd'] or ''},0,100,0\n"
        )
        for kind, _ in variants:
            r = results[kind]
            f.write(
                f"{kind},{r['total_ret']:.4f},{r['max_dd']:.4f},{r['dd_days']},"
                f"{r['ret_per_dd'] or ''},{r['n_round_trips']},{r['pct_in_market']:.2f},"
                f"{len(r['whipsaws'])}\n"
            )
    print(f"\nWrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
