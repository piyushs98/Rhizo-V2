#!/usr/bin/env python3
"""
Offline end-to-end simulation. No network, no API keys, no market hours.

Runs the real scanner, the real risk gates, the real paper broker, the real
position manager and the real exit rules against a synthetic price series.
Everything except the data provider is production code.

Use it to:
  - prove a fresh install works before you touch live data
  - watch the duplicate-suppression path do its job
  - sanity-check a change to scoring, sizing or exit logic in two seconds

    python scripts/simulate.py            # default 60 ticks
    python scripts/simulate.py --ticks 200 --seed 7
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolated database so a simulation never touches your real book.
_TMP = Path(tempfile.mkdtemp(prefix="janus-sim-"))
os.environ.setdefault("DB_PATH", str(_TMP / "sim.db"))
os.environ.setdefault("LOG_DIR", str(_TMP))
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LLM_ENABLED", "false")
os.environ.setdefault("DISCORD_WEBHOOK", "")
os.environ.setdefault("INTER_SYMBOL_SLEEP_S", "0")

from app import clock                                     # noqa: E402
from app.broker.paper import PaperBroker                  # noqa: E402
from app.data.providers import Bar                        # noqa: E402
from app.db import repositories as repo                   # noqa: E402
from app.db.connection import init_db                     # noqa: E402
from app.domain.models import (                           # noqa: E402
    Direction, Market, ScoreCard, SymbolAssessment, Verdict,
)
from app.engine import exit_rules, indicators, risk, scanner, scoring  # noqa: E402
from app.engine.position_manager import manage_all        # noqa: E402


# ===========================================================================
# Synthetic market
# ===========================================================================
class SyntheticSeries:
    """Geometric random walk with a slow drift cycle, so trends actually form."""

    def __init__(self, symbol: str, start: float, rng: random.Random,
                 vol: float = 0.018, drift_period: int = 40):
        self.symbol = symbol
        self.price = start
        self.rng = rng
        self.vol = vol
        self.drift_period = drift_period
        self.t = 0
        self.bars: list[Bar] = []
        for _ in range(80):
            self.step()

    def step(self) -> float:
        self.t += 1
        drift = 0.004 * math.sin(self.t / self.drift_period * 2 * math.pi)
        shock = self.rng.gauss(0, self.vol)
        self.price = max(0.01, self.price * (1 + drift + shock))
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=self.t)
        span = self.price * self.vol
        self.bars.append(Bar(
            ts=ts,
            open=self.price - span * 0.3,
            high=self.price + abs(self.rng.gauss(0, span)),
            low=self.price - abs(self.rng.gauss(0, span)),
            close=self.price,
            volume=1_000_000 * (0.7 + self.rng.random()),
        ))
        self.bars = self.bars[-200:]
        return self.price


class SimulatedAdapter:
    """Implements the MarketAdapter protocol against synthetic series."""

    market = Market.CRYPTO_SPOT
    multiplier = 1.0
    whole_units = False

    def __init__(self, rng: random.Random):
        self.series = {
            "BTC-USD": SyntheticSeries("BTC-USD", 68_000, rng, vol=0.012),
            "ETH-USD": SyntheticSeries("ETH-USD", 3_400, rng, vol=0.018),
            "SOL-USD": SyntheticSeries("SOL-USD", 175, rng, vol=0.026),
        }

    def universe(self) -> list[str]:
        return list(self.series)

    def advance(self) -> None:
        for s in self.series.values():
            s.step()

    def build_context(self, symbols: list[str]) -> dict:
        btc = self.series["BTC-USD"]
        prev = btc.bars[-2].close if len(btc.bars) > 1 else btc.price
        return {"benchmark_change_pct": (btc.price / prev - 1) * 100}

    def assess(self, symbol: str, context: dict) -> SymbolAssessment:
        s = self.series[symbol]
        bars = s.bars
        closes = [b.close for b in bars]
        prev = closes[-2] if len(closes) > 1 else closes[-1]

        liq = scoring.score_spot_liquidity(bars)
        tech = scoring.score_technical(bars, bullish=True)
        sent = scoring.score_sentiment(
            change_pct=(s.price / prev - 1) * 100,
            benchmark_change_pct=(None if symbol == "BTC-USD"
                                  else context.get("benchmark_change_pct")),
            momentum_pct=indicators.momentum_pct(closes, 24),
            volume_ratio=indicators.volume_ratio(bars, 24),
        )
        card = scoring.compose(symbol, liq, tech, sent)
        passes = scoring.meets_threshold(card)

        return SymbolAssessment(
            symbol=symbol, market=self.market, score=card,
            verdict=Verdict.EXECUTE if passes else Verdict.PASS,
            reason=("cleared the threshold" if passes
                    else f"scored {card.total:.1f}"),
            instrument=symbol, ref_price=s.price, entry_price=s.price,
            direction=Direction.LONG_SPOT,
            atr=indicators.atr(bars, 14),
        )

    def mark(self, instrument: str, underlying: str) -> float:
        return self.series[instrument].price


# ===========================================================================
# Runner
# ===========================================================================
def run(ticks: int, seed: int, scan_every: int) -> int:
    rng = random.Random(seed)
    init_db()

    adapter = SimulatedAdapter(rng)
    broker = PaperBroker()
    state = clock.resolve(datetime(2026, 7, 25, 22, 0, tzinfo=clock.ET))

    # Patch the manager's mark lookup onto the simulated adapter.
    import app.engine.position_manager as pm
    pm._mark = lambda pos: adapter.mark(pos.instrument, pos.underlying)

    print(f"\n  Janus Desk — offline simulation")
    print(f"  {ticks} ticks · seed {seed} · scan every {scan_every}\n")
    print(f"  {'tick':>5}  {'equity':>11}  {'cash':>11}  {'open':>4}  "
          f"{'closed':>6}  event")
    print("  " + "─" * 74)

    closed_before = 0
    for tick in range(1, ticks + 1):
        adapter.advance()

        note = ""
        if tick % scan_every == 0:
            outcome = scanner.run_scan(adapter, state, broker)
            if outcome.executed:
                note = f"opened {outcome.executed}"
            else:
                blocked = [a.blocked_by for a in outcome.assessments if a.blocked_by]
                if blocked:
                    note = f"blocked: {', '.join(sorted(set(blocked)))}"

        manage_all(state, broker)

        led = repo.ledger.get()
        closed_now = led["trades_closed"]
        if closed_now > closed_before:
            recent = repo.positions.closed(closed_now - closed_before)
            note = " ".join(
                f"{p.underlying} {p.exit_reason} {p.realized_pnl:+,.0f}"
                for p in recent
            )
            closed_before = closed_now

        if note or tick % 10 == 0:
            s = risk.portfolio_summary()
            print(f"  {tick:>5}  {s['equity']:>11,.2f}  {s['cash']:>11,.2f}  "
                  f"{s['open_count']:>4}  {closed_now:>6}  {note}")

    # ------------------------------------------------------------- results
    s = risk.portfolio_summary()
    print("\n  " + "─" * 74)
    print(f"  Final equity      {s['equity']:>14,.2f}   "
          f"({s['return_pct']:+.2f}%)")
    print(f"  Realized          {s['realized_pnl']:>14,.2f}")
    print(f"  Unrealized        {s['unrealized_pnl']:>14,.2f}")
    print(f"  Max drawdown      {s['drawdown_pct']:>14,.2f}%")
    print(f"  Trades            {s['trades_opened']} opened, "
          f"{s['trades_closed']} closed"
          + (f", {s['win_rate']:.0f}% winners" if s["win_rate"] is not None else ""))
    print(f"  Still open        {s['open_count']}")

    # Exit-reason breakdown proves the manager is actually working.
    from collections import Counter
    reasons = Counter(p.exit_reason for p in repo.positions.closed(500))
    if reasons:
        print("\n  Exits by reason")
        for reason, n in reasons.most_common():
            print(f"    {reason:<18} {n}")

    # Duplicate suppression proof.
    dupes = repo.query(
        "SELECT underlying, COUNT(*) n FROM positions "
        "WHERE status IN ('OPEN','CLOSING') GROUP BY underlying HAVING n > 1"
    ) if hasattr(repo, "query") else []
    from app.db.connection import query
    dupes = query(
        "SELECT underlying, COUNT(*) n FROM positions "
        "WHERE status IN ('OPEN','CLOSING') GROUP BY underlying HAVING n > 1"
    )
    print(f"\n  Duplicate open positions: {len(dupes)}"
          + ("  ← must be zero" if dupes else "  ✓"))

    print(f"\n  Simulation database: {os.environ['DB_PATH']}\n")
    return 1 if dupes else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--scan-every", type=int, default=5)
    args = ap.parse_args()
    sys.exit(run(args.ticks, args.seed, args.scan_every))
