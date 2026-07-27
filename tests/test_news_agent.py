"""
News agent: numeric boundary and every fail-closed path.

The only LLM path that influences a score must emit a float or 0.0.
"""
from __future__ import annotations

import json

import pytest

from app.agents import news
from app.db import repositories as repo
from app.engine import scoring


# ----------------------------------------------------------------- parse_bias
def test_valid_json_bias():
    assert news.parse_bias('{"bias": 0.4, "note": "risk-on"}') == pytest.approx(0.4)


def test_bias_at_upper_bound():
    assert news.parse_bias('{"bias": 1.0}') == 1.0


def test_bias_at_lower_bound():
    assert news.parse_bias('{"bias": -1.0}') == -1.0


def test_bias_just_inside_range():
    assert news.parse_bias('{"bias": 0.999}') == pytest.approx(0.999)
    assert news.parse_bias('{"bias": -0.999}') == pytest.approx(-0.999)


def test_out_of_range_positive_fails_closed():
    assert news.parse_bias('{"bias": 1.01}') is None
    assert news.parse_bias('{"bias": 2.5}') is None


def test_out_of_range_negative_fails_closed():
    assert news.parse_bias('{"bias": -1.01}') is None
    assert news.parse_bias('{"bias": -9}') is None


def test_missing_bias_key_fails_closed():
    assert news.parse_bias('{"note": "hello"}') is None


def test_malformed_json_fails_closed():
    assert news.parse_bias("not json at all") is None
    assert news.parse_bias("{bias: 0.5}") is None
    assert news.parse_bias("") is None


def test_prose_with_embedded_json_still_parses():
    raw = 'Here is my read:\n{"bias": -0.25, "note": "soft"}\nThanks.'
    assert news.parse_bias(raw) == pytest.approx(-0.25)


def test_fenced_json_parses():
    raw = '```json\n{"bias": 0.1, "note": "mild"}\n```'
    assert news.parse_bias(raw) == pytest.approx(0.1)


def test_non_numeric_bias_fails_closed():
    assert news.parse_bias('{"bias": "bullish"}') is None
    assert news.parse_bias('{"bias": null}') is None
    assert news.parse_bias('{"bias": true}') is None


def test_nan_fails_closed():
    # json.loads of NaN is non-standard; simulate via Python object path
    # by feeding a string that becomes invalid float semantics if forced.
    assert news.parse_bias('{"bias": "NaN"}') is None


def test_integer_bias_accepted():
    assert news.parse_bias('{"bias": 0}') == 0.0
    assert news.parse_bias('{"bias": 1}') == 1.0


# --------------------------------------------------------------- persistence
class _S:
    """Tiny stand-in so we do not fight the frozen Settings dataclass."""
    def __init__(self, **kw):
        self.news_enabled = True
        self.llm_available = False
        self.news_bias_ttl_hours = 1.75
        self.news_refresh_interval_s = 1800
        for k, v in kw.items():
            setattr(self, k, v)


def test_run_without_llm_stores_neutral(monkeypatch):
    monkeypatch.setattr(news, "settings", _S(llm_available=False))
    bias = news.run(session_date="2026-07-24", force=True)
    assert bias == 0.0
    row = repo.sentiment.latest()
    assert row is not None
    assert row["bias"] == 0.0
    assert row["session_date"] == "2026-07-24"


def test_run_with_good_model_output(monkeypatch):
    monkeypatch.setattr(news, "settings", _S(llm_available=True))
    monkeypatch.setattr(
        news.llm, "comment",
        lambda *a, **k: json.dumps({"bias": 0.55, "note": "strong open"}),
    )
    bias = news.run(session_date="2026-07-24", force=True)
    assert bias == pytest.approx(0.55)
    assert repo.sentiment.latest_bias() == pytest.approx(0.55)


def test_run_with_malformed_output_fails_closed(monkeypatch):
    monkeypatch.setattr(news, "settings", _S(llm_available=True))
    monkeypatch.setattr(
        news.llm, "comment",
        lambda *a, **k: "The market looks mixed today with several cross-currents.",
    )
    bias = news.run(session_date="2026-07-24", force=True)
    assert bias == 0.0
    assert repo.sentiment.latest()["source"] == "parse_fail"


def test_run_idempotent_within_session(monkeypatch):
    monkeypatch.setattr(news, "settings", _S(llm_available=True))
    calls = {"n": 0}

    def fake_comment(*a, **k):
        calls["n"] += 1
        return '{"bias": 0.3}'

    monkeypatch.setattr(news.llm, "comment", fake_comment)
    a = news.run(session_date="2026-07-25", force=True)
    b = news.run(session_date="2026-07-25", force=False)
    assert a == b == pytest.approx(0.3)
    assert calls["n"] == 1


def test_disabled_news_returns_zero(monkeypatch):
    monkeypatch.setattr(news, "settings", _S(news_enabled=False))
    assert news.run(session_date="2026-07-24", force=True) == 0.0


def test_current_bias_respects_ttl():
    repo.sentiment.store(session_date="2026-07-20", bias=0.8, source="test")
    # max_age_hours=0 rejects every existing row.
    assert repo.sentiment.latest_bias(max_age_hours=0) == 0.0


# ---------------------------------------------------- scoring integration
def test_news_bias_tilts_sentiment_up():
    base, _ = scoring.score_sentiment(change_pct=0.5, benchmark_change_pct=0.0)
    tilted, detail = scoring.score_sentiment(
        change_pct=0.5, benchmark_change_pct=0.0, news_bias=1.0
    )
    assert tilted >= base
    assert detail["news_bias"] == pytest.approx(1.0)


def test_news_bias_tilts_sentiment_down():
    base, _ = scoring.score_sentiment(change_pct=0.5, benchmark_change_pct=0.0)
    tilted, _ = scoring.score_sentiment(
        change_pct=0.5, benchmark_change_pct=0.0, news_bias=-1.0
    )
    assert tilted <= base


def test_news_bias_clamped_before_apply():
    """Out-of-range bias is clamped; score stays in [0, 100]."""
    s, d = scoring.score_sentiment(
        change_pct=0.0, news_bias=5.0  # invalid input; still clamped
    )
    assert 0 <= s <= 100
    assert d["news_bias"] == pytest.approx(1.0)


# ---------------------------------------------------- rolling refresh
def test_refresh_macro_and_crypto_are_separate(monkeypatch):
    monkeypatch.setattr(news, "settings", _S(llm_available=True))
    responses = {
        "MACRO": '{"bias": 0.4, "note": "eq"}',
        "CRYPTO": '{"bias": -0.3, "note": "cx"}',
    }

    def fake_comment(prompt, system="", **kw):
        if "crypto" in system.lower() or "BTC" in prompt:
            return responses["CRYPTO"]
        return responses["MACRO"]

    monkeypatch.setattr(news.llm, "comment", fake_comment)
    m = news.refresh("MACRO", session_date="2026-07-27", force=True)
    c = news.refresh("CRYPTO", session_date="2026-07-27", force=True)
    assert m == pytest.approx(0.4)
    assert c == pytest.approx(-0.3)
    assert repo.sentiment.latest_bias(scope="MACRO") == pytest.approx(0.4)
    assert repo.sentiment.latest_bias(scope="CRYPTO") == pytest.approx(-0.3)


def test_refresh_appends_history(monkeypatch):
    monkeypatch.setattr(news, "settings", _S(llm_available=True))
    n = {"v": 0.1}

    def fake_comment(*a, **k):
        n["v"] += 0.1
        return json.dumps({"bias": round(n["v"], 2)})

    monkeypatch.setattr(news.llm, "comment", fake_comment)
    news.refresh("MACRO", session_date="2026-07-27", force=True)
    news.refresh("MACRO", session_date="2026-07-27", force=True)
    rows = repo.sentiment.recent(limit=10, scope="MACRO")
    assert len(rows) >= 2


def test_refresh_never_raises(monkeypatch):
    monkeypatch.setattr(news, "settings", _S(llm_available=True))
    monkeypatch.setattr(
        news.llm, "comment",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    # Must not raise even if the LLM chain blows up (refresh catches).
    assert news.refresh("MACRO", session_date="2026-07-27", force=True) == 0.0


def test_bias_age_seconds():
    repo.sentiment.store(
        session_date="2026-07-27", bias=0.2, source="test", scope="MACRO"
    )
    age = news.bias_age_seconds("MACRO")
    assert age is not None and age >= 0
    assert news.bias_age_seconds("CRYPTO") is None


def test_status_reports_both_scopes():
    repo.sentiment.store(
        session_date="2026-07-27", bias=0.1, source="t", scope="MACRO"
    )
    repo.sentiment.store(
        session_date="2026-07-27", bias=-0.1, source="t", scope="CRYPTO"
    )
    st = news.status()
    assert "macro" in st and "crypto" in st
    assert st["macro"]["fresh"] is True
    assert st["crypto"]["fresh"] is True


def test_refresh_skips_when_fresh(monkeypatch):
    monkeypatch.setattr(
        news, "settings",
        _S(llm_available=True, news_refresh_interval_s=3600),
    )
    calls = {"n": 0}

    def fake_comment(*a, **k):
        calls["n"] += 1
        return '{"bias": 0.5}'

    monkeypatch.setattr(news.llm, "comment", fake_comment)
    news.refresh("MACRO", session_date="2026-07-27", force=True)
    news.refresh("MACRO", session_date="2026-07-27", force=False)
    assert calls["n"] == 1
