"""Backtest metrics and CSV writers."""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from statistics import mean


@dataclass
class TradeRow:
    open_date: str
    close_date: str
    symbol: str
    direction: str
    entry: float
    exit: float
    reason: str
    pnl: float
    hold_hours: float
    score: float
    market: str = "EQUITY_SHARE"


@dataclass
class Metrics:
    label: str
    starting_capital: float
    ending_equity: float
    total_return_pct: float
    annualized_return_pct: float | None
    win_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    largest_win: float | None
    largest_loss: float | None
    profit_factor: float | None
    max_drawdown_pct: float
    max_dd_duration_days: float
    n_trades: int
    trades_per_month: float | None
    longest_losing_streak: int
    exits_by_reason: dict[str, int] = field(default_factory=dict)
    months: dict[str, float] = field(default_factory=dict)
    best_trade_pnl: float | None = None
    return_without_best_trade_pct: float | None = None
    notes: str = ""


def _equity_curve_drawdown(curve: list[tuple[datetime, float]]
                           ) -> tuple[float, float]:
    if not curve:
        return 0.0, 0.0
    peak = curve[0][1]
    max_dd = 0.0
    dd_start: datetime | None = None
    max_dur = 0.0
    for ts, eq in curve:
        if eq >= peak:
            peak = eq
            dd_start = None
        else:
            dd = (eq / peak - 1.0) * 100.0
            if dd < max_dd:
                max_dd = dd
            if dd_start is None:
                dd_start = ts
            else:
                max_dur = max(max_dur, (ts - dd_start).total_seconds() / 86400.0)
    return max_dd, max_dur


def compute_metrics(
    *,
    label: str,
    starting_capital: float,
    ending_equity: float,
    trades: list[TradeRow],
    equity_curve: list[tuple[datetime, float]],
    period_days: float,
) -> Metrics:
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    n = len(trades)
    win_rate = (len(wins) / n * 100.0) if n else None
    avg_win = mean(wins) if wins else None
    avg_loss = mean(losses) if losses else None
    largest_win = max(wins) if wins else None
    largest_loss = min(losses) if losses else None
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else float("inf"))

    streak = longest = 0
    for t in trades:
        if t.pnl <= 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0

    reasons: dict[str, int] = defaultdict(int)
    months: dict[str, float] = defaultdict(float)
    for t in trades:
        reasons[t.reason] += 1
        months[t.close_date[:7]] += t.pnl

    max_dd, max_dd_days = _equity_curve_drawdown(equity_curve)
    total_ret = (ending_equity / starting_capital - 1.0) * 100.0 if starting_capital else 0.0
    years = period_days / 365.25 if period_days > 0 else 0.0
    ann = None
    if years > 0 and starting_capital > 0 and ending_equity > 0:
        ann = ((ending_equity / starting_capital) ** (1 / years) - 1.0) * 100.0

    tpm = None
    if period_days > 0:
        tpm = n / (period_days / 30.4375)

    best = max((t.pnl for t in trades), default=None)
    without_best = None
    if best is not None and n >= 1:
        without_best = (ending_equity - best) / starting_capital * 100.0 - 100.0 + 100.0
        without_best = ((ending_equity - best) / starting_capital - 1.0) * 100.0

    notes = []
    if n < 50:
        notes.append(f"only {n} trades — under ~50 is statistically thin")
    if months:
        top_m = max(months, key=lambda k: abs(months[k]))
        share = abs(months[top_m]) / max(1e-9, sum(abs(v) for v in months.values()))
        if share > 0.5 and n >= 3:
            notes.append(
                f"PnL concentrated: month {top_m} is {share*100:.0f}% of |monthly PnL|"
            )

    return Metrics(
        label=label,
        starting_capital=starting_capital,
        ending_equity=round(ending_equity, 2),
        total_return_pct=round(total_ret, 2),
        annualized_return_pct=round(ann, 2) if ann is not None else None,
        win_rate=round(win_rate, 1) if win_rate is not None else None,
        avg_win=round(avg_win, 2) if avg_win is not None else None,
        avg_loss=round(avg_loss, 2) if avg_loss is not None else None,
        largest_win=round(largest_win, 2) if largest_win is not None else None,
        largest_loss=round(largest_loss, 2) if largest_loss is not None else None,
        profit_factor=round(pf, 3) if pf is not None and pf != float("inf") else pf,
        max_drawdown_pct=round(max_dd, 2),
        max_dd_duration_days=round(max_dd_days, 1),
        n_trades=n,
        trades_per_month=round(tpm, 2) if tpm is not None else None,
        longest_losing_streak=longest,
        exits_by_reason=dict(reasons),
        months={k: round(v, 2) for k, v in sorted(months.items())},
        best_trade_pnl=round(best, 2) if best is not None else None,
        return_without_best_trade_pct=(
            round(without_best, 2) if without_best is not None else None
        ),
        notes="; ".join(notes),
    )


def required_win_rate(stop_pct: float, target_pct: float,
                      fee_rt: float = 0.0) -> float:
    """Break-even win rate for fixed % stop/target after round-trip cost."""
    nw = target_pct - fee_rt
    nl = stop_pct + fee_rt
    if nw + nl <= 0:
        return 1.0
    return nl / (nw + nl)


def write_trades_csv(path: Path, trades: list[TradeRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not trades:
        path.write_text("open_date,close_date,symbol,direction,entry,exit,reason,pnl,hold_hours,score,market\n")
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(trades[0]).keys()))
        w.writeheader()
        for t in trades:
            w.writerow(asdict(t))


def write_metrics_csv(path: Path, rows: list[Metrics]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    flat = []
    for m in rows:
        d = {
            "label": m.label,
            "starting_capital": m.starting_capital,
            "ending_equity": m.ending_equity,
            "total_return_pct": m.total_return_pct,
            "annualized_return_pct": m.annualized_return_pct,
            "win_rate": m.win_rate,
            "avg_win": m.avg_win,
            "avg_loss": m.avg_loss,
            "largest_win": m.largest_win,
            "largest_loss": m.largest_loss,
            "profit_factor": m.profit_factor,
            "max_drawdown_pct": m.max_drawdown_pct,
            "max_dd_duration_days": m.max_dd_duration_days,
            "n_trades": m.n_trades,
            "trades_per_month": m.trades_per_month,
            "longest_losing_streak": m.longest_losing_streak,
            "exits_by_reason": str(m.exits_by_reason),
            "best_trade_pnl": m.best_trade_pnl,
            "return_without_best_trade_pct": m.return_without_best_trade_pct,
            "notes": m.notes,
        }
        flat.append(d)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(flat[0].keys()))
        w.writeheader()
        w.writerows(flat)


def write_monthly_csv(path: Path, months: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month", "pnl"])
        for k, v in sorted(months.items()):
            w.writerow([k, v])
