"""
SQLite access layer.

WAL mode means the engine can write while the web process reads, with no
locking contention between them. That is what lets the dashboard and the
trading loop live in separate OS processes sharing one file - which in turn
is what removes v1's `--workers 1` constraint and the whole class of
port-collision and double-spawn failures.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1

_local = threading.local()


def utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")


def get_connection() -> sqlite3.Connection:
    """One connection per thread. Safe to call anywhere."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.db_path, timeout=8.0, isolation_level=None)
        _configure(conn)
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Explicit transaction. Commits on success, rolls back on any exception."""
    conn = get_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return list(get_connection().execute(sql, params).fetchall())


def query_one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    return get_connection().execute(sql, params).fetchone()


def execute(sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
    return get_connection().execute(sql, params)


def init_db() -> None:
    """Idempotent. Safe to run on every boot."""
    conn = get_connection()
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, utcnow()),
    )
    conn.execute(
        """INSERT OR IGNORE INTO ledger
           (id, starting_capital, cash, peak_equity, updated_at)
           VALUES (1, ?, ?, ?, ?)""",
        (settings.starting_capital, settings.starting_capital,
         settings.starting_capital, utcnow()),
    )


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
