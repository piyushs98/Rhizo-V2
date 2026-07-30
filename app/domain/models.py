"""
Domain vocabulary. Plain dataclasses, no ORM, no framework.

Both markets share these types. An equity option position and a BTC spot
position differ only in `market`, `multiplier`, and how their instrument
symbol is constructed. Everything downstream - risk gates, exit rules, the
ledger, the dashboard - treats them identically.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class Market(str, Enum):
    EQUITY_OPTION = "EQUITY_OPTION"
    EQUITY_SHARE = "EQUITY_SHARE"
    CRYPTO_SPOT = "CRYPTO_SPOT"


class Direction(str, Enum):
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    LONG_SHARE = "LONG_SHARE"
    LONG_SPOT = "LONG_SPOT"


class Status(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class Verdict(str, Enum):
    EXECUTE = "EXECUTE"
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    VWAP_BREAK = "VWAP_BREAK"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_STOP = "TIME_STOP"
    SESSION_FLATTEN = "SESSION_FLATTEN"
    MANUAL = "MANUAL"
    RISK_HALT = "RISK_HALT"
    STALE_DATA = "STALE_DATA"


# The only transitions the system permits. Enforced in the repository.
LEGAL_TRANSITIONS: dict[Status, set[Status]] = {
    Status.PENDING: {Status.OPEN, Status.REJECTED},
    Status.OPEN: {Status.CLOSING, Status.CLOSED},
    Status.CLOSING: {Status.CLOSED, Status.OPEN},
    Status.CLOSED: set(),
    Status.REJECTED: set(),
}


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


# ------------------------------------------------------------------ scoring
@dataclass
class ScoreCard:
    """
    Deterministic scoring output. Every field is a number derived from
    numbers.

    Post-mortem #4: v1 leaked LLM prose into this path - a substring match on
    the word "warning" inside a manager's narrative silently suppressed the
    technical bonus and capped scores at ~50-65 for an unknown period. No
    string from a language model reaches this class. Commentary lives on
    `SymbolAssessment.commentary` and is display-only.
    """
    symbol: str
    liquidity: float = 0.0
    technical: float = 0.0
    sentiment: float = 0.0
    weights: dict[str, float] = field(
        default_factory=lambda: {"liquidity": 30.0, "technical": 40.0, "sentiment": 30.0}
    )
    inputs: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        w = self.weights
        return round(
            self.liquidity * w["liquidity"] / 100.0
            + self.technical * w["technical"] / 100.0
            + self.sentiment * w["sentiment"] / 100.0,
            2,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total"] = self.total
        return d


@dataclass
class SymbolAssessment:
    """One symbol's full evaluation within one scan."""
    symbol: str
    market: Market
    score: ScoreCard
    verdict: Verdict = Verdict.PASS
    reason: str = ""
    blocked_by: str | None = None
    instrument: str | None = None
    ref_price: float | None = None      # underlying / spot price
    entry_price: float | None = None    # premium per contract, or spot
    direction: Direction | None = None
    atr: float | None = None
    commentary: str = ""                # LLM, advisory only
    exit_plan: "ExitPlan | None" = None  # adapter-supplied plan (e.g. BTC scalp)
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------- exit plan
@dataclass
class ExitPlan:
    """
    Written once, at entry. Evaluated by pure functions on every manage tick.

    This is the answer to "how do I know whether to hold or close". The rules
    are fixed at the moment of entry so the decision is never re-litigated by
    a model that might read the tape differently five minutes later.

    Scalp fields (`scalp`, `r_unit`, `vwap_floor`) are set only for the BTC
    multi-layer plan. Equity options leave them at defaults.
    """
    stop_price: float
    target_price: float
    trail_activate_at: float | None = None   # price at which trailing arms
    trail_giveback_pct: float = 0.15
    trail_high_water: float | None = None
    time_stop_ts: datetime | None = None
    scalp: bool = False
    r_unit: float | None = None              # $ risk unit (ATR-scaled)
    vwap_floor: float | None = None          # live floor for VWAP-break exits

    @classmethod
    def build(
        cls,
        entry_price: float,
        *,
        stop_pct: float,
        target_pct: float,
        trail_activate_pct: float,
        trail_giveback_pct: float,
        max_hold_hours: float,
        now: datetime | None = None,
    ) -> "ExitPlan":
        now = now or utcnow()
        return cls(
            stop_price=round(entry_price * (1 - stop_pct), 6),
            target_price=round(entry_price * (1 + target_pct), 6),
            trail_activate_at=round(entry_price * (1 + trail_activate_pct), 6),
            trail_giveback_pct=trail_giveback_pct,
            trail_high_water=None,
            time_stop_ts=now + timedelta(hours=max_hold_hours),
        )


# ---------------------------------------------------------------- position
@dataclass
class Position:
    position_id: str
    idempotency_key: str
    market: Market
    underlying: str
    instrument: str
    direction: Direction
    status: Status
    quantity: float
    multiplier: float
    entry_price: float | None = None
    entry_ts: datetime | None = None
    entry_notional: float | None = None
    exit_price: float | None = None
    exit_ts: datetime | None = None
    exit_reason: str | None = None
    plan: ExitPlan | None = None
    mark_price: float | None = None
    mark_ts: datetime | None = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    fees: float = 0.0
    open_scan_id: str | None = None
    entry_score: float | None = None
    session_key: str | None = None
    notes: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:16]

    @staticmethod
    def make_idempotency_key(
        market: Market, underlying: str, direction: Direction, session_key: str
    ) -> str:
        """
        One position per (market, underlying, direction) per shift.

        This string is the UNIQUE column in the positions table. A second scan
        in the same session producing the same signal hits a constraint
        violation and is discarded. The bug where every scan stacked another
        contract onto the same name is impossible by construction.
        """
        return f"{market.value}:{underlying}:{direction.value}:{session_key}"

    def notional(self, price: float) -> float:
        return price * self.quantity * self.multiplier

    def compute_unrealized(self, mark: float) -> float:
        if self.entry_price is None:
            return 0.0
        return round((mark - self.entry_price) * self.quantity * self.multiplier, 2)

    def pnl_pct(self, mark: float | None = None) -> float:
        px = mark if mark is not None else self.mark_price
        if not self.entry_price or px is None:
            return 0.0
        return round((px - self.entry_price) / self.entry_price * 100.0, 2)

    def progress_to_target(self, mark: float | None = None) -> float:
        """
        Where the mark sits between stop and target, 0..1.
        Drives the distance-to-exit bar on the dashboard.
        """
        px = mark if mark is not None else self.mark_price
        if not self.plan or px is None:
            return 0.0
        lo, hi = self.plan.stop_price, self.plan.target_price
        if hi <= lo:
            return 0.0
        return max(0.0, min(1.0, (px - lo) / (hi - lo)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "idempotency_key": self.idempotency_key,
            "market": self.market.value,
            "underlying": self.underlying,
            "instrument": self.instrument,
            "direction": self.direction.value,
            "status": self.status.value,
            "quantity": self.quantity,
            "multiplier": self.multiplier,
            "entry_price": self.entry_price,
            "entry_ts": iso(self.entry_ts),
            "entry_notional": self.entry_notional,
            "exit_price": self.exit_price,
            "exit_ts": iso(self.exit_ts),
            "exit_reason": self.exit_reason,
            "stop_price": self.plan.stop_price if self.plan else None,
            "target_price": self.plan.target_price if self.plan else None,
            "trail_high_water": self.plan.trail_high_water if self.plan else None,
            "time_stop_ts": iso(self.plan.time_stop_ts) if self.plan else None,
            "scalp": bool(self.plan.scalp) if self.plan else False,
            "r_unit": self.plan.r_unit if self.plan else None,
            "vwap_floor": self.plan.vwap_floor if self.plan else None,
            "r_multiple": self.r_multiple(),
            "mark_price": self.mark_price,
            "mark_ts": iso(self.mark_ts),
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "pnl_pct": self.pnl_pct(),
            "progress": self.progress_to_target(),
            "fees": self.fees,
            "open_scan_id": self.open_scan_id,
            "entry_score": self.entry_score,
            "session_key": self.session_key,
            "notes": self.notes,
            "meta": self.meta,
        }

    def r_multiple(self, mark: float | None = None) -> float | None:
        """Live R-multiple for scalp positions; None when not applicable."""
        if not self.plan or not self.plan.scalp or not self.plan.r_unit:
            return None
        if not self.entry_price or self.plan.r_unit <= 0:
            return None
        px = mark if mark is not None else self.mark_price
        if px is None:
            return None
        return round((px - self.entry_price) / self.plan.r_unit, 3)


@dataclass
class OrderIntent:
    """What the scanner hands to the executor. Not yet a position."""
    market: Market
    underlying: str
    instrument: str
    direction: Direction
    quantity: float
    multiplier: float
    limit_price: float
    session_key: str
    scan_id: str
    score: float
    plan: ExitPlan
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        return Position.make_idempotency_key(
            self.market, self.underlying, self.direction, self.session_key
        )

    @property
    def notional(self) -> float:
        return self.limit_price * self.quantity * self.multiplier


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))
