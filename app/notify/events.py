"""
One message format for every position lifecycle event.

Opens, stop losses, take profits, trailing stops, VWAP breaks, time stops,
and manual closes all go through here. Transport stays in discord.py — this
module only shapes the text and hands it off, so a second alerting path
never has to be invented.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.models import ExitReason, Position
from app.engine.exit_rules import describe_plan
from app.notify import discord

if TYPE_CHECKING:
    from app.domain.models import SymbolAssessment


def format_open(
    position: Position,
    *,
    fill_price: float,
    score_total: float | None = None,
    liquidity: float | None = None,
    technical: float | None = None,
    sentiment: float | None = None,
) -> str:
    notional = fill_price * position.quantity * position.multiplier
    lines = [
        f"**Opened {position.underlying}** · {position.direction.value}",
        f"`{position.instrument}`",
        f"{position.quantity:g} @ {fill_price:,.4f}  "
        f"(notional {notional:,.2f})",
    ]
    if score_total is not None:
        L = f"{liquidity:.0f}" if liquidity is not None else "—"
        T = f"{technical:.0f}" if technical is not None else "—"
        S = f"{sentiment:.0f}" if sentiment is not None else "—"
        lines.append(f"Score {score_total:.1f} (L {L} / T {T} / S {S})")
    if position.plan and position.plan.scalp:
        r = position.plan.r_unit
        lines.append(f"Scalp · R={r:,.4f}" if r else "Scalp")
    lines.append(describe_plan(position))
    return "\n".join(lines)


def format_close(
    position: Position,
    reason: ExitReason | str,
    detail: str = "",
) -> str:
    reason_s = reason.value if isinstance(reason, ExitReason) else str(reason)
    entry = position.entry_price or 0.0
    exit_px = position.exit_price if position.exit_price is not None else (
        position.mark_price or 0.0
    )
    pnl = position.realized_pnl
    pct = position.pnl_pct(exit_px) if entry else 0.0
    lines = [
        f"**Closed {position.underlying}** · {reason_s}",
        f"`{position.instrument}`",
        f"{position.quantity:g} @ {exit_px:,.4f} "
        f"(entry {entry:,.4f})",
        f"Realized **{pnl:+,.2f}** ({pct:+.1f}%)",
    ]
    if position.plan and position.plan.scalp and position.plan.r_unit:
        r_mult = position.r_multiple(exit_px)
        if r_mult is not None:
            lines.append(f"R-multiple {r_mult:+.2f}R")
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def notify_open(
    position: Position,
    *,
    fill_price: float,
    assessment: "SymbolAssessment | None" = None,
) -> None:
    score_total = liq = tech = sent = None
    if assessment is not None:
        score_total = assessment.score.total
        liq = assessment.score.liquidity
        tech = assessment.score.technical
        sent = assessment.score.sentiment
    elif position.entry_score is not None:
        score_total = position.entry_score

    msg = format_open(
        position,
        fill_price=fill_price,
        score_total=score_total,
        liquidity=liq,
        technical=tech,
        sentiment=sent,
    )
    try:
        discord.info(msg, channel="execution")
    except Exception:
        # discord.send already never raises; this is belt-and-braces so a
        # formatter bug cannot kill a fill.
        pass


def notify_close(
    position: Position,
    reason: ExitReason | str,
    detail: str = "",
) -> None:
    msg = format_close(position, reason, detail)
    won = (position.realized_pnl or 0.0) > 0
    level = "INFO" if won else "WARN"
    try:
        discord.send(msg, level, channel="execution")  # type: ignore[arg-type]
    except Exception:
        pass
