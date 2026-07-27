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
SCHEMA_VERSION = 3

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


def _current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_meta"
        ).fetchone()
        return int(row["v"] or 0) if row else 0
    except sqlite3.OperationalError:
        return 0


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """
    Additive upgrades for databases created under an older schema.

    CREATE TABLE IF NOT EXISTS covers new tables. Columns on existing tables
    must be added via ALTER TABLE — SQLite has no IF NOT EXISTS for columns.
    Table rebuilds preserve existing rows.
    """
    version = _current_version(conn)
    tables = _tables(conn)

    # --- v2: scalp fields on positions + sentiment table -------------------
    if version < 2:
        if "positions" in tables:
            _add_column_if_missing(
                conn, "positions", "scalp", "INTEGER NOT NULL DEFAULT 0"
            )
            _add_column_if_missing(conn, "positions", "vwap_floor", "REAL")
            _add_column_if_missing(conn, "positions", "r_unit", "REAL")

        conn.execute(
            """CREATE TABLE IF NOT EXISTS sentiment (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date  TEXT    NOT NULL,
                bias          REAL    NOT NULL,
                source        TEXT    NOT NULL DEFAULT '',
                raw_json      TEXT    NOT NULL DEFAULT '',
                note          TEXT    NOT NULL DEFAULT '',
                created_at    TEXT    NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_sentiment_created "
            "ON sentiment(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_sentiment_session "
            "ON sentiment(session_date)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta (version, applied_at) VALUES (?, ?)",
            (2, utcnow()),
        )
        version = max(version, 2)
        tables = _tables(conn)

    # --- v3: append-only sentiment with scope; rebuild preserves rows ------
    if version < 3:
        if "sentiment" in tables:
            cols = _column_names(conn, "sentiment")
            if "scope" not in cols:
                # Rebuild: drop any accidental UNIQUE, add scope.
                conn.execute(
                    """CREATE TABLE sentiment_v3 (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_date  TEXT    NOT NULL,
                        scope         TEXT    NOT NULL DEFAULT 'MACRO',
                        bias          REAL    NOT NULL,
                        source        TEXT    NOT NULL DEFAULT '',
                        raw_json      TEXT    NOT NULL DEFAULT '',
                        note          TEXT    NOT NULL DEFAULT '',
                        created_at    TEXT    NOT NULL
                    )"""
                )
                conn.execute(
                    """INSERT INTO sentiment_v3
                       (id, session_date, scope, bias, source, raw_json, note, created_at)
                       SELECT id, session_date, 'MACRO', bias, source, raw_json, note, created_at
                       FROM sentiment"""
                )
                conn.execute("DROP TABLE sentiment")
                conn.execute("ALTER TABLE sentiment_v3 RENAME TO sentiment")
            else:
                # Already has scope (fresh schema.sql) — ensure indexes only.
                pass
        else:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS sentiment (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_date  TEXT    NOT NULL,
                    scope         TEXT    NOT NULL DEFAULT 'MACRO',
                    bias          REAL    NOT NULL,
                    source        TEXT    NOT NULL DEFAULT '',
                    raw_json      TEXT    NOT NULL DEFAULT '',
                    note          TEXT    NOT NULL DEFAULT '',
                    created_at    TEXT    NOT NULL
                )"""
            )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_sentiment_created "
            "ON sentiment(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_sentiment_session "
            "ON sentiment(session_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_sentiment_scope "
            "ON sentiment(scope, created_at DESC)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta (version, applied_at) VALUES (?, ?)",
            (3, utcnow()),
        )


def init_db() -> None:
    """Idempotent. Safe to run on every boot. Upgrades existing DBs in place."""
    conn = get_connection()
    conn.executescript(SCHEMA_PATH.read_text())
    _apply_migrations(conn)
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
