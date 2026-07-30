"""
The scan pipeline. Market-agnostic.

Given an adapter it walks that adapter's universe, scores each symbol, runs
the risk gates, and opens what survives. It does not know whether it is
looking at NVDA options or BTC spot.

Two invariants carried over from v1, both of which were load-bearing:

  - Every symbol runs inside its own try. One bad chain, one hung socket,
    one malformed row cannot end the scan for the other nine names.
  - Every symbol's outcome is recorded, including failures and refusals,
    with the gate that stopped it. "Why didn't it fire" is a query, not an
    archaeology project.

Selection is best-of-N: the whole universe is scored first, then EXECUTE
candidates are opened in descending score order up to the position limit.
First-past-the-post (open as you iterate) is wrong when the book only fits
2–3 names.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.broker.base import Broker
from app.clock import SessionState, session_key
from app.config import settings
from app.data.providers import DataUnavailable
from app.db import repositories as repo
from app.domain.models import ExitPlan, Market, OrderIntent, SymbolAssessment, Verdict
from app.engine import risk
from app.llm import chain as llm
from app.llm import prompts
from app.markets.adapters import MarketAdapter
from app.resilience.circuit_breaker import BreakerOpen
from app.resilience.timeouts import CallTimeout

log = logging.getLogger("scanner")


@dataclass
class ScanOutcome:
    scan_id: str
    scanned: int = 0
    ok: int = 0
    failed: int = 0
    executed: int = 0
    duration_ms: int = 0
    assessments: list[SymbolAssessment] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.assessments is None:
            self.assessments = []


def new_scan_id() -> str:
    return f"{datetime.now(tz=timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def run_scan(adapter: MarketAdapter, state: SessionState, broker: Broker) -> ScanOutcome:
    scan_id = new_scan_id()
    started = time.monotonic()
    universe = adapter.universe()
    skey = session_key(state)

    repo.scans.start(scan_id, state.regime.value, adapter.market.value,
                     skey, len(universe))
    log.info("scan %s starting | %s | %d symbols",
             scan_id, adapter.market.value, len(universe))

    outcome = ScanOutcome(scan_id=scan_id, scanned=len(universe))

    # Shared context once per scan, never per symbol. Batched where possible.
    try:
        context = adapter.build_context(universe)
    except Exception as exc:
        log.warning("scan %s: context unavailable (%s); continuing", scan_id, exc)
        context = {}

    # --- Pass 1: score the whole universe (no fills) ---------------------
    chains_fetched = 0
    for symbol in universe:
        try:
            assessment = adapter.assess(symbol, context)
            outcome.ok += 1
        except (DataUnavailable, CallTimeout, BreakerOpen) as exc:
            outcome.failed += 1
            _record_failure(scan_id, symbol, adapter.market, str(exc))
            log.warning("scan %s: %s unavailable - %s", scan_id, symbol, exc)
            continue
        except Exception as exc:
            outcome.failed += 1
            _record_failure(scan_id, symbol, adapter.market, f"unexpected: {exc}")
            log.exception("scan %s: %s raised", scan_id, symbol)
            continue

        # Advisory commentary. Failure here changes nothing.
        if settings.llm_available and assessment.verdict is Verdict.EXECUTE:
            assessment.commentary = llm.comment(
                prompts.assessment_comment(assessment)
            )

        outcome.assessments.append(assessment)

        fetched = bool((assessment.detail or {}).get("chain_fetched"))
        if fetched:
            chains_fetched += 1
            time.sleep(settings.inter_symbol_sleep_s)

    # --- Pass 2: open best-of-N by score, up to position limit -----------
    candidates = sorted(
        (a for a in outcome.assessments if a.verdict is Verdict.EXECUTE),
        key=lambda a: a.score.total,
        reverse=True,
    )
    if candidates:
        log.info(
            "scan %s best-of-N: %d candidates ranked %s",
            scan_id,
            len(candidates),
            ", ".join(f"{a.symbol}:{a.score.total:.1f}" for a in candidates),
        )

    for assessment in candidates:
        # Position limit may have been hit by an earlier (higher-score) fill.
        if repo.positions.open_count() >= settings.max_open_positions:
            assessment.verdict = Verdict.BLOCKED
            assessment.blocked_by = "MAX_OPEN"
            assessment.reason = (
                f"best-of-N: lower-ranked than open book; "
                f"limit is {settings.max_open_positions}."
            )
            log.info("scan %s: %s skipped (MAX_OPEN after higher ranks)",
                     scan_id, assessment.symbol)
            continue

        opened = _try_open(assessment, adapter, state, broker, scan_id)
        if opened:
            outcome.executed += 1

    # Persist every assessment with its final verdict / blocked_by.
    for assessment in outcome.assessments:
        repo.scans.record_result(scan_id, assessment)

    duration = int((time.monotonic() - started) * 1000)
    outcome.duration_ms = duration
    status = "OK" if outcome.failed == 0 else ("PARTIAL" if outcome.ok else "ABORTED")
    repo.scans.finish(
        scan_id, ok=outcome.ok, failed=outcome.failed,
        executed=outcome.executed, duration_ms=duration, status=status,
    )
    reqs = context.get("requests", "?")
    log.info(
        "scan %s done in %dms | ok %d | failed %d | opened %d | "
        "context_requests=%s | chains_fetched=%d",
        scan_id, duration, outcome.ok, outcome.failed, outcome.executed,
        reqs, chains_fetched,
    )
    return outcome


# ---------------------------------------------------------------- internals
def _record_failure(scan_id: str, symbol: str, market: Market, reason: str) -> None:
    from app.domain.models import ScoreCard

    repo.scans.record_result(
        scan_id,
        SymbolAssessment(
            symbol=symbol, market=market, score=ScoreCard(symbol=symbol),
            verdict=Verdict.ERROR, reason=reason[:400],
        ),
    )


def _try_open(
    assessment: SymbolAssessment,
    adapter: MarketAdapter,
    state: SessionState,
    broker: Broker,
    scan_id: str,
) -> bool:
    """Risk gates, sizing, fill, persist. Returns True if a position opened."""
    if assessment.entry_price is None or assessment.direction is None:
        return False

    skey = session_key(state)
    from app.domain.models import Position

    key = Position.make_idempotency_key(
        adapter.market, assessment.symbol, assessment.direction, skey
    )

    decision = risk.check(
        market=adapter.market,
        underlying=assessment.symbol,
        direction=assessment.direction,
        idempotency_key=key,
        entry_price=assessment.entry_price,
        multiplier=adapter.multiplier,
        whole_units=adapter.whole_units,
    )

    if not decision.allowed:
        assessment.verdict = Verdict.BLOCKED
        assessment.blocked_by = decision.gate
        assessment.reason = decision.reason
        log.info("scan %s: %s blocked by %s - %s",
                 scan_id, assessment.symbol, decision.gate, decision.reason)
        return False

    qty = risk.size_position(
        decision.max_notional, assessment.entry_price,
        adapter.multiplier, whole_units=adapter.whole_units,
    )
    if qty <= 0:
        assessment.verdict = Verdict.BLOCKED
        assessment.blocked_by = "SIZE_ZERO"
        assessment.reason = (
            f"budget {decision.max_notional:,.2f} does not cover one unit at "
            f"{assessment.entry_price:,.4f}"
        )
        return False

    # Prefer an adapter-supplied plan (BTC scalp). Otherwise build from
    # market-specific percentage defaults.
    if assessment.exit_plan is not None:
        plan = assessment.exit_plan
    else:
        plan = ExitPlan.build(
            assessment.entry_price, **settings.exit_params(adapter.market.value)
        )

    intent = OrderIntent(
        market=adapter.market,
        underlying=assessment.symbol,
        instrument=assessment.instrument or assessment.symbol,
        direction=assessment.direction,
        quantity=qty,
        multiplier=adapter.multiplier,
        limit_price=assessment.entry_price,
        session_key=skey,
        scan_id=scan_id,
        score=assessment.score.total,
        plan=plan,
        meta=assessment.detail,
    )

    if settings.dry_run:
        assessment.reason = f"dry run: would have bought {qty} x {intent.instrument}"
        log.info("scan %s: %s", scan_id, assessment.reason)
        return False

    fill = broker.buy(intent)
    position, created = repo.positions.open_position(intent, fill.price)

    if not created:
        # Belt and braces. The risk gate should already have caught this;
        # if it did not, the UNIQUE constraint did.
        assessment.verdict = Verdict.BLOCKED
        assessment.blocked_by = "DUPLICATE"
        assessment.reason = "position already exists for this signal"
        log.warning("scan %s: duplicate suppressed for %s", scan_id, assessment.symbol)
        return False

    from app.notify import events as trade_events
    trade_events.notify_open(position, fill_price=fill.price, assessment=assessment)
    return True
