"""
Repositories. All SQL lives here; nothing above this layer writes a query.

The position repository is the safety-critical piece. Two rules it enforces
that v1 enforced nowhere:

  1. `open_position` is idempotent. A duplicate idempotency key returns the
     existing position instead of creating a second one.
  2. Status changes go through `_transition`, which rejects anything not in
     LEGAL_TRANSITIONS. A CLOSED position can never reopen; an OPEN position
     can never be opened twice.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.db.connection import execute, get_connection, query, query_one, tx, utcnow
from app.domain.models import (
    LEGAL_TRANSITIONS,
    Direction,
    ExitPlan,
    Market,
    OrderIntent,
    Position,
    Status,
    dumps,
)


class TransitionError(RuntimeError):
    """Attempted an illegal position state change."""


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ===========================================================================
# Positions
# ===========================================================================
class PositionRepo:

    # ------------------------------------------------------------ mapping
    @staticmethod
    def _row_to_position(r: sqlite3.Row) -> Position:
        plan = None
        if r["stop_price"] is not None:
            # Keys may be missing on pre-migration rows; default safely.
            keys = r.keys()
            scalp = bool(r["scalp"]) if "scalp" in keys and r["scalp"] else False
            vwap_floor = r["vwap_floor"] if "vwap_floor" in keys else None
            r_unit = r["r_unit"] if "r_unit" in keys else None
            plan = ExitPlan(
                stop_price=r["stop_price"],
                target_price=r["target_price"],
                trail_activate_at=r["trail_activate_at"],
                trail_giveback_pct=r["trail_giveback_pct"] or 0.15,
                trail_high_water=r["trail_high_water"],
                time_stop_ts=_dt(r["time_stop_ts"]),
                scalp=scalp,
                vwap_floor=vwap_floor,
                r_unit=r_unit,
            )
        return Position(
            position_id=r["position_id"],
            idempotency_key=r["idempotency_key"],
            market=Market(r["market"]),
            underlying=r["underlying"],
            instrument=r["instrument"],
            direction=Direction(r["direction"]),
            status=Status(r["status"]),
            quantity=r["quantity"],
            multiplier=r["multiplier"],
            entry_price=r["entry_price"],
            entry_ts=_dt(r["entry_ts"]),
            entry_notional=r["entry_notional"],
            exit_price=r["exit_price"],
            exit_ts=_dt(r["exit_ts"]),
            exit_reason=r["exit_reason"],
            plan=plan,
            mark_price=r["mark_price"],
            mark_ts=_dt(r["mark_ts"]),
            unrealized_pnl=r["unrealized_pnl"] or 0.0,
            realized_pnl=r["realized_pnl"] or 0.0,
            fees=r["fees"] or 0.0,
            open_scan_id=r["open_scan_id"],
            entry_score=r["entry_score"],
            session_key=r["session_key"],
            notes=r["notes"] or "",
            meta=json.loads(r["meta_json"] or "{}"),
        )

    # ------------------------------------------------------------- reads
    def get(self, position_id: str) -> Position | None:
        r = query_one("SELECT * FROM positions WHERE position_id = ?", (position_id,))
        return self._row_to_position(r) if r else None

    def by_key(self, key: str) -> Position | None:
        r = query_one("SELECT * FROM positions WHERE idempotency_key = ?", (key,))
        return self._row_to_position(r) if r else None

    def open_positions(self, market: Market | None = None) -> list[Position]:
        sql = "SELECT * FROM positions WHERE status IN ('OPEN','CLOSING','PENDING')"
        params: tuple = ()
        if market:
            sql += " AND market = ?"
            params = (market.value,)
        sql += " ORDER BY entry_ts ASC"
        return [self._row_to_position(r) for r in query(sql, params)]

    def open_count(self) -> int:
        r = query_one(
            "SELECT COUNT(*) AS n FROM positions "
            "WHERE status IN ('OPEN','CLOSING','PENDING')"
        )
        return int(r["n"]) if r else 0

    def open_count_for(self, underlying: str) -> int:
        r = query_one(
            "SELECT COUNT(*) AS n FROM positions WHERE underlying = ? "
            "AND status IN ('OPEN','CLOSING','PENDING')",
            (underlying,),
        )
        return int(r["n"]) if r else 0

    def opened_since(self, since_iso: str) -> int:
        r = query_one(
            "SELECT COUNT(*) AS n FROM positions WHERE entry_ts >= ?", (since_iso,)
        )
        return int(r["n"]) if r else 0

    def last_close_ts(self, underlying: str) -> datetime | None:
        r = query_one(
            "SELECT exit_ts FROM positions WHERE underlying = ? AND status='CLOSED' "
            "ORDER BY exit_ts DESC LIMIT 1",
            (underlying,),
        )
        return _dt(r["exit_ts"]) if r and r["exit_ts"] else None

    def closed(self, limit: int = 100) -> list[Position]:
        rows = query(
            "SELECT * FROM positions WHERE status='CLOSED' "
            "ORDER BY exit_ts DESC LIMIT ?",
            (limit,),
        )
        return [self._row_to_position(r) for r in rows]

    def realized_pnl_since(self, since_iso: str) -> float:
        r = query_one(
            "SELECT COALESCE(SUM(realized_pnl),0) AS p FROM positions "
            "WHERE status='CLOSED' AND exit_ts >= ?",
            (since_iso,),
        )
        return float(r["p"]) if r else 0.0

    # ------------------------------------------------------------- writes
    def open_position(self, intent: OrderIntent, fill_price: float) -> tuple[Position, bool]:
        """
        Create a position from an intent. Returns (position, created).

        `created=False` means an identical signal already produced a position
        this session and this call was a no-op. That is the normal, expected
        outcome of re-scanning a name you already hold - not an error.
        """
        existing = self.by_key(intent.idempotency_key)
        if existing is not None:
            return existing, False

        pid = Position.new_id()
        now = utcnow()
        plan = intent.plan
        notional = fill_price * intent.quantity * intent.multiplier

        try:
            with tx() as conn:
                conn.execute(
                    """INSERT INTO positions (
                        position_id, idempotency_key, market, underlying, instrument,
                        direction, status, quantity, multiplier,
                        entry_price, entry_ts, entry_notional,
                        stop_price, target_price, trail_activate_at,
                        trail_giveback_pct, trail_high_water, time_stop_ts,
                        scalp, vwap_floor, r_unit,
                        mark_price, mark_ts, unrealized_pnl, realized_pnl,
                        open_scan_id, entry_score, session_key, meta_json,
                        created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        pid, intent.idempotency_key, intent.market.value,
                        intent.underlying, intent.instrument, intent.direction.value,
                        Status.OPEN.value, intent.quantity, intent.multiplier,
                        fill_price, now, notional,
                        plan.stop_price, plan.target_price, plan.trail_activate_at,
                        plan.trail_giveback_pct, None,
                        plan.time_stop_ts.isoformat() if plan.time_stop_ts else None,
                        1 if plan.scalp else 0, plan.vwap_floor, plan.r_unit,
                        fill_price, now, 0.0, 0.0,
                        intent.scan_id, intent.score, intent.session_key,
                        dumps(intent.meta), now, now,
                    ),
                )
                conn.execute(
                    """INSERT INTO position_events
                       (position_id, ts, event, from_status, to_status, price, detail)
                       VALUES (?,?,?,?,?,?,?)""",
                    (pid, now, "OPENED", None, Status.OPEN.value, fill_price,
                     f"scan {intent.scan_id} score {intent.score}"),
                )
        except sqlite3.IntegrityError:
            # Lost a race against a concurrent identical signal. Fine.
            found = self.by_key(intent.idempotency_key)
            if found:
                return found, False
            raise

        pos = self.get(pid)
        assert pos is not None
        return pos, True

    def _transition(
        self, conn: sqlite3.Connection, pos: Position, to: Status
    ) -> None:
        if to not in LEGAL_TRANSITIONS[pos.status]:
            raise TransitionError(
                f"{pos.position_id}: {pos.status.value} -> {to.value} is not allowed"
            )

    def mark(self, position_id: str, price: float) -> Position | None:
        """Refresh the live mark and unrealized PnL. Also raises the trail."""
        pos = self.get(position_id)
        if pos is None or pos.status not in (Status.OPEN, Status.CLOSING):
            return pos

        now = utcnow()
        unreal = pos.compute_unrealized(price)
        new_hw = pos.plan.trail_high_water if pos.plan else None
        trail_raised = False

        if pos.plan and pos.plan.trail_activate_at is not None:
            if price >= pos.plan.trail_activate_at:
                if new_hw is None or price > new_hw:
                    new_hw = price
                    trail_raised = True

        with tx() as conn:
            conn.execute(
                """UPDATE positions SET mark_price=?, mark_ts=?, unrealized_pnl=?,
                   trail_high_water=?, updated_at=? WHERE position_id=?""",
                (price, now, unreal, new_hw, now, position_id),
            )
            if trail_raised:
                conn.execute(
                    """INSERT INTO position_events
                       (position_id, ts, event, price, detail)
                       VALUES (?,?,?,?,?)""",
                    (position_id, now, "TRAIL_RAISED", price,
                     f"high water {new_hw}"),
                )
        return self.get(position_id)

    def set_vwap_floor(self, position_id: str, floor: float) -> Position | None:
        """Refresh the live VWAP floor on a scalp position. Pure write."""
        pos = self.get(position_id)
        if pos is None or pos.status not in (Status.OPEN, Status.CLOSING):
            return pos
        if not pos.plan or not pos.plan.scalp:
            return pos
        now = utcnow()
        execute(
            "UPDATE positions SET vwap_floor=?, updated_at=? WHERE position_id=?",
            (floor, now, position_id),
        )
        return self.get(position_id)

    def request_close(self, position_id: str, reason: str) -> bool:
        pos = self.get(position_id)
        if pos is None:
            return False
        if pos.status is Status.CLOSING:
            return True
        with tx() as conn:
            self._transition(conn, pos, Status.CLOSING)
            now = utcnow()
            conn.execute(
                "UPDATE positions SET status=?, updated_at=? WHERE position_id=?",
                (Status.CLOSING.value, now, position_id),
            )
            conn.execute(
                """INSERT INTO position_events
                   (position_id, ts, event, from_status, to_status, detail)
                   VALUES (?,?,?,?,?,?)""",
                (position_id, now, "CLOSE_REQUESTED", pos.status.value,
                 Status.CLOSING.value, reason),
            )
        return True

    def close(self, position_id: str, exit_price: float, reason: str,
              fees: float = 0.0) -> Position | None:
        pos = self.get(position_id)
        if pos is None or pos.status is Status.CLOSED:
            return pos

        realized = round(
            (exit_price - (pos.entry_price or 0.0)) * pos.quantity * pos.multiplier
            - fees, 2,
        )
        now = utcnow()
        with tx() as conn:
            self._transition(conn, pos, Status.CLOSED)
            conn.execute(
                """UPDATE positions SET status=?, exit_price=?, exit_ts=?,
                   exit_reason=?, realized_pnl=?, unrealized_pnl=0,
                   mark_price=?, mark_ts=?, fees=fees+?, updated_at=?
                   WHERE position_id=?""",
                (Status.CLOSED.value, exit_price, now, reason, realized,
                 exit_price, now, fees, now, position_id),
            )
            conn.execute(
                """INSERT INTO position_events
                   (position_id, ts, event, from_status, to_status, price, detail)
                   VALUES (?,?,?,?,?,?,?)""",
                (position_id, now, "CLOSED", pos.status.value, Status.CLOSED.value,
                 exit_price, f"{reason} pnl={realized}"),
            )
        return self.get(position_id)

    def events(self, position_id: str) -> list[dict]:
        rows = query(
            "SELECT * FROM position_events WHERE position_id=? ORDER BY id ASC",
            (position_id,),
        )
        return [dict(r) for r in rows]


# ===========================================================================
# Scans
# ===========================================================================
class ScanRepo:
    def start(self, scan_id: str, regime: str, market: str, session_key: str,
              symbols_total: int) -> None:
        execute(
            """INSERT INTO scans
               (scan_id, started_at, regime, market, session_key, symbols_total)
               VALUES (?,?,?,?,?,?)""",
            (scan_id, utcnow(), regime, market, session_key, symbols_total),
        )

    def finish(self, scan_id: str, *, ok: int, failed: int, executed: int,
               duration_ms: int, status: str, note: str = "") -> None:
        execute(
            """UPDATE scans SET finished_at=?, symbols_ok=?, symbols_failed=?,
               executed=?, duration_ms=?, status=?, note=? WHERE scan_id=?""",
            (utcnow(), ok, failed, executed, duration_ms, status, note, scan_id),
        )

    def record_result(self, scan_id: str, a: Any) -> None:
        execute(
            """INSERT INTO scan_results
               (scan_id, ts, symbol, market, total_score, liquidity, technical,
                sentiment, verdict, blocked_by, reason, instrument, ref_price,
                commentary, detail_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scan_id, utcnow(), a.symbol, a.market.value, a.score.total,
             a.score.liquidity, a.score.technical, a.score.sentiment,
             a.verdict.value, a.blocked_by, a.reason, a.instrument, a.ref_price,
             a.commentary[:2000], dumps({"inputs": a.score.inputs,
                                         "notes": a.score.notes,
                                         "detail": a.detail})),
        )

    def latest(self) -> dict | None:
        r = query_one("SELECT * FROM scans ORDER BY started_at DESC LIMIT 1")
        if not r:
            return None
        scan = dict(r)
        scan["results"] = [
            dict(x) for x in query(
                "SELECT * FROM scan_results WHERE scan_id=? "
                "ORDER BY total_score DESC", (r["scan_id"],)
            )
        ]
        return scan

    def recent(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in query(
            "SELECT * FROM scans ORDER BY started_at DESC LIMIT ?", (limit,)
        )]


# ===========================================================================
# Ledger
# ===========================================================================
class LedgerRepo:
    def get(self) -> dict:
        r = query_one("SELECT * FROM ledger WHERE id = 1")
        return dict(r) if r else {}

    def debit(self, amount: float, fees: float = 0.0) -> None:
        execute(
            """UPDATE ledger SET cash = cash - ?, fees_paid = fees_paid + ?,
               trades_opened = trades_opened + 1, updated_at = ? WHERE id=1""",
            (amount + fees, fees, utcnow()),
        )

    def credit(self, amount: float, realized: float, fees: float = 0.0) -> None:
        execute(
            """UPDATE ledger SET cash = cash + ?, realized_pnl = realized_pnl + ?,
               fees_paid = fees_paid + ?, trades_closed = trades_closed + 1,
               wins = wins + ?, losses = losses + ?, updated_at = ?
               WHERE id=1""",
            (amount - fees, realized, fees,
             1 if realized > 0 else 0, 1 if realized <= 0 else 0, utcnow()),
        )

    def snapshot(self, open_value: float, open_count: int) -> None:
        led = self.get()
        equity = led.get("cash", 0.0) + open_value
        execute(
            """INSERT INTO equity_curve
               (ts, cash, open_value, equity, realized_pnl, open_count)
               VALUES (?,?,?,?,?,?)""",
            (utcnow(), led.get("cash", 0.0), open_value, equity,
             led.get("realized_pnl", 0.0), open_count),
        )
        if equity > led.get("peak_equity", 0.0):
            execute("UPDATE ledger SET peak_equity=? WHERE id=1", (equity,))

    def curve(self, limit: int = 500) -> list[dict]:
        rows = query(
            "SELECT ts, equity, realized_pnl, open_count FROM equity_curve "
            "ORDER BY ts DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in reversed(rows)]


# ===========================================================================
# Commands, heartbeats, events, kv
# ===========================================================================
class CommandRepo:
    def enqueue(self, kind: str, payload: dict | None = None,
                source: str = "dashboard") -> int:
        cur = execute(
            """INSERT INTO commands (created_at, kind, payload_json, source)
               VALUES (?,?,?,?)""",
            (utcnow(), kind, dumps(payload or {}), source),
        )
        return int(cur.lastrowid or 0)

    def claim_next(self) -> dict | None:
        with tx() as conn:
            r = conn.execute(
                "SELECT * FROM commands WHERE status='QUEUED' ORDER BY id LIMIT 1"
            ).fetchone()
            if not r:
                return None
            conn.execute(
                "UPDATE commands SET status='RUNNING', picked_at=? WHERE id=?",
                (utcnow(), r["id"]),
            )
        return dict(r)

    def complete(self, cmd_id: int, ok: bool, result: str = "") -> None:
        execute(
            "UPDATE commands SET status=?, finished_at=?, result=? WHERE id=?",
            ("DONE" if ok else "FAILED", utcnow(), result[:500], cmd_id),
        )

    def recent(self, limit: int = 25) -> list[dict]:
        return [dict(r) for r in query(
            "SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,)
        )]


class HeartbeatRepo:
    def beat(self, component: str, pid: int, detail: str = "") -> None:
        execute(
            """INSERT INTO heartbeats (component, ts, pid, detail)
               VALUES (?,?,?,?)
               ON CONFLICT(component) DO UPDATE SET ts=excluded.ts,
                 pid=excluded.pid, detail=excluded.detail""",
            (component, utcnow(), pid, detail),
        )

    def all(self) -> list[dict]:
        return [dict(r) for r in query("SELECT * FROM heartbeats")]

    def age_seconds(self, component: str) -> float | None:
        r = query_one("SELECT ts FROM heartbeats WHERE component=?", (component,))
        if not r:
            return None
        ts = _dt(r["ts"])
        if not ts:
            return None
        return (datetime.now(tz=timezone.utc) - ts).total_seconds()


class EventRepo:
    def add(self, level: str, channel: str, message: str,
            meta: dict | None = None) -> None:
        execute(
            """INSERT INTO events (ts, level, channel, message, meta_json)
               VALUES (?,?,?,?,?)""",
            (utcnow(), level, channel, message[:1000], dumps(meta or {})),
        )

    def recent(self, limit: int = 60, level: str | None = None) -> list[dict]:
        if level:
            rows = query(
                "SELECT * FROM events WHERE level=? ORDER BY id DESC LIMIT ?",
                (level, limit),
            )
        else:
            rows = query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def prune(self, keep: int = 5000) -> None:
        execute(
            "DELETE FROM events WHERE id < (SELECT MAX(id) - ? FROM events)",
            (keep,),
        )


class KVRepo:
    def get(self, key: str, default: str = "") -> str:
        r = query_one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def set(self, key: str, value: str) -> None:
        execute(
            """INSERT INTO kv (key, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                 updated_at=excluded.updated_at""",
            (key, value, utcnow()),
        )

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key, "")
        return default if not v else v.lower() in {"1", "true", "yes", "on"}


# ===========================================================================
# Sentiment (rolling news bias — append-only)
# ===========================================================================
class SentimentRepo:
    """
    Append-only history of numeric bias readings. Scoring reads the latest
    float per scope; it never sees the raw LLM text.
    """

    def store(
        self,
        *,
        session_date: str,
        bias: float,
        source: str = "",
        raw_json: str = "",
        note: str = "",
        scope: str = "MACRO",
    ) -> None:
        sc = (scope or "MACRO").upper()
        if sc not in {"MACRO", "CRYPTO"}:
            sc = "MACRO"
        execute(
            """INSERT INTO sentiment
               (session_date, scope, bias, source, raw_json, note, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (session_date, sc, float(bias), source, raw_json[:4000],
             note[:500], utcnow()),
        )

    def latest(
        self,
        *,
        max_age_hours: float | None = None,
        scope: str | None = None,
    ) -> dict[str, Any] | None:
        """Most recent row (optionally scoped), or None if missing / past TTL."""
        if scope:
            r = query_one(
                "SELECT * FROM sentiment WHERE scope=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                ((scope or "MACRO").upper(),),
            )
        else:
            r = query_one(
                "SELECT * FROM sentiment ORDER BY created_at DESC, id DESC LIMIT 1"
            )
        if not r:
            return None
        row = dict(r)
        if max_age_hours is not None:
            if max_age_hours <= 0:
                return None
            created = _dt(row.get("created_at"))
            if created is None:
                return None
            age_h = (datetime.now(tz=timezone.utc) - created).total_seconds() / 3600.0
            if age_h > max_age_hours:
                return None
        return row

    def latest_bias(
        self,
        *,
        max_age_hours: float | None = None,
        scope: str | None = None,
    ) -> float:
        row = self.latest(max_age_hours=max_age_hours, scope=scope)
        if row is None:
            return 0.0
        try:
            return float(row["bias"])
        except (TypeError, ValueError, KeyError):
            return 0.0

    def recent(self, limit: int = 10, scope: str | None = None) -> list[dict]:
        if scope:
            return [dict(r) for r in query(
                "SELECT * FROM sentiment WHERE scope=? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                ((scope or "MACRO").upper(), limit),
            )]
        return [dict(r) for r in query(
            "SELECT * FROM sentiment ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )]

    def prune(self, keep: int = 500) -> None:
        """Keep the newest `keep` rows; drop the rest. Append-only hygiene."""
        execute(
            "DELETE FROM sentiment WHERE id < "
            "(SELECT COALESCE(MAX(id), 0) - ? FROM sentiment)",
            (keep,),
        )


positions = PositionRepo()
scans = ScanRepo()
ledger = LedgerRepo()
commands = CommandRepo()
heartbeats = HeartbeatRepo()
events = EventRepo()
kv = KVRepo()
sentiment = SentimentRepo()
