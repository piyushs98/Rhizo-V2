"""Prompt templates for advisory commentary."""
from __future__ import annotations

from app.domain.models import SymbolAssessment


def assessment_comment(a: SymbolAssessment) -> str:
    inputs = "\n".join(f"  {k}: {v}" for k, v in sorted(a.score.inputs.items()))
    return (
        f"Symbol: {a.symbol} ({a.market.value})\n"
        f"Reference price: {a.ref_price}\n"
        f"Direction read: {a.direction.value if a.direction else 'none'}\n"
        f"Pillar scores - liquidity {a.score.liquidity}, "
        f"technical {a.score.technical}, sentiment {a.score.sentiment}, "
        f"total {a.score.total}\n"
        f"Verdict: {a.verdict.value} ({a.reason})\n"
        f"Raw inputs:\n{inputs}\n\n"
        f"In two or three sentences: what does this setup look like, and what "
        f"would invalidate it?"
    )


def session_summary(regime: str, executed: int, scanned: int,
                    top: list[tuple[str, float]]) -> str:
    board = ", ".join(f"{s} {v:.0f}" for s, v in top) or "nothing of note"
    return (
        f"Desk shift: {regime}. Scanned {scanned} symbols, opened {executed} "
        f"positions. Highest scores: {board}.\n\n"
        f"Two sentences on the shape of this session."
    )
