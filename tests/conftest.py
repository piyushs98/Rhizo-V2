"""Test fixtures. A fresh database per test, no network, no clock surprises."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Must be set before anything imports app.config.
_TMP = Path(tempfile.mkdtemp(prefix="janus-test-"))
os.environ["DB_PATH"] = str(_TMP / "test.db")
os.environ["LOG_DIR"] = str(_TMP / "logs")
os.environ["LOG_LEVEL"] = "CRITICAL"
os.environ["DISCORD_WEBHOOK"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["LLM_ENABLED"] = "false"
os.environ["INTER_SYMBOL_SLEEP_S"] = "0"
# Tests never hit the network. Dummy Alpaca keys satisfy validation if a
# test flips MARKET_DATA_PROVIDER; the default remains yahoo.
os.environ.setdefault("MARKET_DATA_PROVIDER", "yahoo")
os.environ.setdefault("ALPACA_KEY_ID", "PK_TEST_DUMMY")
os.environ.setdefault("ALPACA_SECRET_KEY", "SK_TEST_DUMMY")
os.environ.setdefault("NEWS_BIAS_TTL_HOURS", "1.75")
os.environ.setdefault("NEWS_REFRESH_INTERVAL_S", "1800")

import pytest  # noqa: E402

from app.db import connection  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Every test starts from an empty schema."""
    db = Path(os.environ["DB_PATH"])
    conn = getattr(connection._local, "conn", None)
    if conn is not None:
        conn.close()
        connection._local.conn = None
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    connection.init_db()
    yield
    conn = getattr(connection._local, "conn", None)
    if conn is not None:
        conn.close()
        connection._local.conn = None


@pytest.fixture
def bars():
    """A clean uptrend with a mild pullback. Deterministic."""
    from datetime import datetime, timedelta, timezone

    from app.data.providers import Bar

    out, price = [], 100.0
    base = datetime(2026, 1, 2, tzinfo=timezone.utc)
    for i in range(60):
        price *= 1.004 if i % 7 else 0.994
        out.append(Bar(
            ts=base + timedelta(days=i),
            open=price * 0.997, high=price * 1.010,
            low=price * 0.991, close=price,
            volume=1_000_000 + (i % 5) * 120_000,
        ))
    return out
