"""
Deploy config: universe lock, storage guard, /healthz.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import CANONICAL_EQUITY_UNIVERSE, Settings


def _settings(**env) -> Settings:
    """Build a Settings from an isolated env overlay."""
    base = {
        "ENV": "development",
        "EPHEMERAL_STORAGE_ACK": "",
        "ALLOW_CUSTOM_UNIVERSE": "",
        "EQUITY_UNIVERSE": ",".join(CANONICAL_EQUITY_UNIVERSE),
        "DB_PATH": "/tmp/janus-test-deploy.db",
    }
    base.update({k: str(v) for k, v in env.items()})
    with patch.dict(os.environ, base, clear=False):
        return Settings()


# ----------------------------------------------------------- universe lock
def test_canonical_universe_has_ten_tickers():
    assert len(CANONICAL_EQUITY_UNIVERSE) == 10
    assert set(CANONICAL_EQUITY_UNIVERSE) == {
        "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "SPY", "QQQ", "IWM",
    }


def test_default_universe_is_canonical():
    s = _settings()
    assert set(s.equity_universe) == set(CANONICAL_EQUITY_UNIVERSE)


def test_custom_universe_rejected_by_default():
    s = _settings(EQUITY_UNIVERSE="AAPL,MSFT", ALLOW_CUSTOM_UNIVERSE="")
    errs = s.validate()
    assert any("EQUITY_UNIVERSE" in e for e in errs)


def test_custom_universe_allowed_with_flag():
    s = _settings(EQUITY_UNIVERSE="AAPL,MSFT", ALLOW_CUSTOM_UNIVERSE="true")
    errs = s.validate()
    assert not any("EQUITY_UNIVERSE" in e for e in errs)


def test_canonical_universe_any_order_ok():
    shuffled = ",".join(reversed(CANONICAL_EQUITY_UNIVERSE))
    s = _settings(EQUITY_UNIVERSE=shuffled)
    assert not any("EQUITY_UNIVERSE" in e for e in s.validate())


def test_extra_ticker_rejected():
    extra = ",".join(CANONICAL_EQUITY_UNIVERSE + ["NFLX"])
    s = _settings(EQUITY_UNIVERSE=extra)
    assert any("canonical" in e.lower() or "EQUITY_UNIVERSE" in e for e in s.validate())


# ---------------------------------------------------------- storage guard
def test_production_ephemeral_path_requires_ack():
    s = _settings(
        ENV="production",
        DB_PATH="/opt/render/project/src/data/janus.db",
        EPHEMERAL_STORAGE_ACK="",
    )
    errs = s.validate()
    assert any("EPHEMERAL_STORAGE_ACK" in e for e in errs)


def test_production_ephemeral_with_ack_ok():
    s = _settings(
        ENV="production",
        DB_PATH="/opt/render/project/src/data/janus.db",
        EPHEMERAL_STORAGE_ACK="true",
    )
    errs = s.validate()
    assert not any("EPHEMERAL_STORAGE_ACK" in e for e in errs)


def test_production_var_data_path_skips_ack():
    s = _settings(
        ENV="production",
        DB_PATH="/var/data/janus.db",
        EPHEMERAL_STORAGE_ACK="",
    )
    errs = s.validate()
    assert not any("EPHEMERAL_STORAGE_ACK" in e for e in errs)


def test_development_skips_storage_guard():
    s = _settings(
        ENV="development",
        DB_PATH="./data/janus.db",
        EPHEMERAL_STORAGE_ACK="",
    )
    errs = s.validate()
    assert not any("EPHEMERAL_STORAGE_ACK" in e for e in errs)


# ---------------------------------------------------- discord webhook alias
def test_discord_webhook_url_alias():
    s = _settings(DISCORD_WEBHOOK="", DISCORD_WEBHOOK_URL="https://hooks.example/a")
    assert s.discord_webhook == "https://hooks.example/a"


def test_discord_webhook_primary_wins():
    s = _settings(
        DISCORD_WEBHOOK="https://hooks.example/primary",
        DISCORD_WEBHOOK_URL="https://hooks.example/alias",
    )
    assert s.discord_webhook == "https://hooks.example/primary"


# ---------------------------------------------------------------- /healthz
def test_healthz_always_200():
    from app.web.server import app
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_503_without_engine_heartbeat():
    from app.web.server import app
    client = TestClient(app)
    r = client.get("/health")
    # Fresh DB: no heartbeat yet.
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


def test_health_200_with_fresh_heartbeat():
    from app.db import repositories as repo
    from app.web.server import app
    repo.heartbeats.beat("engine", pid=1, detail="{}")
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["engine"] == "alive"


def test_api_sentiment_empty():
    from app.web.server import app
    client = TestClient(app)
    r = client.get("/api/sentiment")
    assert r.status_code == 200
    body = r.json()
    assert "macro" in body and "crypto" in body
    assert body["macro"]["bias"] == 0.0
    assert body["macro"]["fresh"] is False


def test_api_sentiment_with_row():
    from app.db import repositories as repo
    from app.web.server import app
    repo.sentiment.store(
        session_date="2026-07-24", bias=0.42, note="test", scope="MACRO"
    )
    client = TestClient(app)
    r = client.get("/api/sentiment")
    assert r.status_code == 200
    body = r.json()
    assert body["macro"]["fresh"] is True
    assert body["macro"]["bias"] == pytest.approx(0.42)


def test_overview_includes_sentiment():
    from app.web.server import app
    client = TestClient(app)
    r = client.get("/api/overview")
    assert r.status_code == 200
    body = r.json()
    assert "sentiment" in body
    assert "request_budget" in body
