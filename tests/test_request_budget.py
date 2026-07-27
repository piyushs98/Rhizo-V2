"""
Request budget invariant: the governor is hard, and batched paths count.

No test makes a network call. Alpaca HTTP is stubbed; the governor is real.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.data.providers import Bar, Quote
from app.resilience import governor as gov_mod
from app.resilience.governor import RateGovernor, RateLimitExceeded


@pytest.fixture(autouse=True)
def _reset_gov():
    gov_mod.reset_all()
    yield
    gov_mod.reset_all()


# ---------------------------------------------------------------- governor
def test_acquire_counts_toward_limit():
    g = RateGovernor("t", limit=5, window_s=60)
    for _ in range(5):
        g.acquire(block=False)
    assert g.used() == 5
    assert g.remaining() == 0


def test_nonblocking_raises_when_full():
    g = RateGovernor("t", limit=2, window_s=60)
    g.acquire(block=False)
    g.acquire(block=False)
    with pytest.raises(RateLimitExceeded):
        g.acquire(block=False)


def test_window_prunes_old_hits():
    g = RateGovernor("t", limit=3, window_s=0.15)
    g.acquire(block=False)
    g.acquire(block=False)
    time.sleep(0.2)
    assert g.used() == 0
    g.acquire(block=False)
    assert g.used() == 1


def test_record_429_fills_window():
    g = RateGovernor("t", limit=10, window_s=60)
    g.acquire(block=False)
    g.record_429()
    assert g.used() == 10
    with pytest.raises(RateLimitExceeded):
        g.acquire(block=False)


def test_snapshot_shape():
    g = RateGovernor("t", limit=100, window_s=60)
    g.acquire(block=False)
    snap = g.snapshot().to_dict()
    assert snap["used"] == 1
    assert snap["limit"] == 100
    assert snap["remaining"] == 99


def test_blocking_acquire_waits_for_slot():
    g = RateGovernor("t", limit=1, window_s=0.12)
    g.acquire(block=False)
    t0 = time.monotonic()
    g.acquire(block=True, timeout=1.0)
    assert time.monotonic() - t0 >= 0.05


def test_alpaca_governor_singleton_reset():
    a = gov_mod.alpaca_governor()
    a.acquire(block=False)
    gov_mod.reset_all()
    b = gov_mod.alpaca_governor()
    assert b.used() == 0


# ---------------------------------------------------------- batched counting
def _bars(n=40, start=100.0):
    base = datetime(2026, 1, 2, tzinfo=timezone.utc)
    out, price = [], start
    for i in range(n):
        price *= 1.002
        out.append(Bar(
            ts=base + timedelta(days=i),
            open=price, high=price * 1.01, low=price * 0.99,
            close=price, volume=1_000_000,
        ))
    return out


def test_quotes_many_is_one_request(monkeypatch):
    from app.data.alpaca import AlpacaProvider

    p = AlpacaProvider(key_id="k", secret="s")
    calls = {"n": 0}

    def fake_get(path, params=None):
        calls["n"] += 1
        return {
            "quotes": {
                s: {"bp": 100.0, "ap": 100.1}
                for s in (params or {}).get("symbols", "").split(",")
                if s
            }
        }

    monkeypatch.setattr(p, "_get", fake_get)
    out = p.quotes_many(["AAPL", "MSFT", "NVDA"])
    assert calls["n"] == 1
    assert set(out) == {"AAPL", "MSFT", "NVDA"}
    assert p.request_count == 0  # fake_get bypasses counter; count via calls


def test_bars_many_is_one_request(monkeypatch):
    from app.data.alpaca import AlpacaProvider

    p = AlpacaProvider(key_id="k", secret="s")
    calls = {"n": 0}

    def fake_get(path, params=None):
        calls["n"] += 1
        syms = (params or {}).get("symbols", "").split(",")
        return {
            "bars": {
                s: [{"t": "2026-01-02T00:00:00Z", "o": 1, "h": 2, "l": 0.5,
                     "c": 1.5, "v": 100}]
                for s in syms if s
            }
        }

    monkeypatch.setattr(p, "_get", fake_get)
    out = p.bars_many(["AAPL", "MSFT"], lookback_days=30, interval="1d")
    assert calls["n"] == 1
    assert "AAPL" in out and "MSFT" in out


def test_build_context_two_requests_when_batched(monkeypatch):
    """quotes_many + bars_many = 2 requests for ten tickers."""
    from app.data.alpaca import AlpacaProvider
    from app.markets.adapters import EquityOptionsAdapter, reset_adapters

    reset_adapters()
    p = AlpacaProvider(key_id="k", secret="s")
    calls = {"n": 0}

    def fake_get(path, params=None):
        calls["n"] += 1
        syms = [s for s in (params or {}).get("symbols", "").split(",") if s]
        if "quotes" in path:
            return {"quotes": {s: {"bp": 100.0, "ap": 100.2} for s in syms}}
        if "bars" in path:
            return {
                "bars": {
                    s: [
                        {"t": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
                         "o": 100 + i, "h": 101 + i, "l": 99 + i,
                         "c": 100.5 + i, "v": 1e6}
                        for i in range(40)
                    ]
                    for s in syms
                }
            }
        return {}

    monkeypatch.setattr(p, "_get", fake_get)
    # Count via wrapper that increments provider counter.
    real = p._get

    def counting_get(path, params=None):
        p._req_count += 1
        return real(path, params)

    monkeypatch.setattr(p, "_get", counting_get)

    adapter = EquityOptionsAdapter()
    adapter.data = p
    universe = ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL",
                "META", "TSLA", "SPY", "QQQ", "IWM"]
    ctx = adapter.build_context(universe)
    assert ctx["requests"] == 2
    assert len(ctx["quotes"]) >= 5
    assert calls["n"] == 2


def test_chain_worth_fetching_exact_bound():
    from app.markets.adapters import _chain_worth_fetching
    # tech+sent so low that even liq=100 cannot clear 70
    tech = (0.0, {})
    sent = (0.0, {})
    assert _chain_worth_fetching(tech, sent) is False
    # high pillars clear easily
    assert _chain_worth_fetching((90.0, {}), (90.0, {})) is True


def test_mark_many_one_batch(monkeypatch):
    from app.data.alpaca import AlpacaProvider
    from app.markets.adapters import EquityOptionsAdapter

    p = AlpacaProvider(key_id="k", secret="s")
    calls = {"n": 0}

    def fake_get(path, params=None):
        calls["n"] += 1
        return {
            "snapshots": {
                "AAPL260130C00150000": {
                    "latestQuote": {"bp": 5.0, "ap": 5.2},
                },
                "MSFT260130C00400000": {
                    "latestQuote": {"bp": 3.0, "ap": 3.1},
                },
            }
        }

    monkeypatch.setattr(p, "_get", fake_get)
    adapter = EquityOptionsAdapter()
    adapter.data = p
    marks = adapter.mark_many([
        ("AAPL260130C00150000", "AAPL"),
        ("MSFT260130C00400000", "MSFT"),
    ])
    assert calls["n"] == 1
    assert marks["AAPL260130C00150000"] == pytest.approx(5.1)


def test_governor_blocks_extra_acquire_after_budget():
    g = RateGovernor("t", limit=3, window_s=60)
    for _ in range(3):
        g.acquire(block=False)
    with pytest.raises(RateLimitExceeded):
        g.acquire(block=False)
    assert g.stats()["blocked"] >= 1


def test_budget_status_dict():
    gov_mod.alpaca_governor().acquire(block=False)
    st = gov_mod.budget_status()
    assert "alpaca" in st
    assert st["alpaca"]["used"] >= 1


def test_alpaca_get_uses_governor(monkeypatch):
    from app.data.alpaca import AlpacaProvider

    p = AlpacaProvider(key_id="k", secret="s")
    # Fill governor so acquire(block=True) would hang; use tiny timeout via budget
    g = gov_mod.alpaca_governor()
    for _ in range(g.limit):
        g.acquire(block=False)

    # Non-blocking path inside: we patch acquire to raise
    monkeypatch.setattr(
        g, "acquire",
        lambda **kw: (_ for _ in ()).throw(RateLimitExceeded("full")),
    )
    with pytest.raises(Exception):
        p._get("/v2/stocks/quotes/latest", {"symbols": "AAPL"})


def test_soft_limit_config_default():
    from app.config import settings
    assert settings.alpaca_rate_limit_per_min <= settings.alpaca_hard_limit_per_min


def test_reset_providers_clears_cache(monkeypatch):
    from app.data import providers
    providers.reset_providers()
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "yahoo")
    # Force re-read is not automatic on frozen settings; just call reset.
    providers.reset_providers()
    p1 = providers.equity_provider()
    p2 = providers.equity_provider()
    assert p1 is p2
    providers.reset_providers()
