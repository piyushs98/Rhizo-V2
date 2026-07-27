"""
Discord lifecycle events: open/close formatters, chunking, failure isolation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.domain.models import (
    Direction, ExitPlan, ExitReason, Market, Position, ScoreCard,
    Status, SymbolAssessment, Verdict,
)
from app.notify import discord
from app.notify import events as trade_events


NOW = datetime(2026, 7, 24, 15, 0, tzinfo=timezone.utc)


def _pos(**kw):
    defaults = dict(
        position_id="p1", idempotency_key="k1", market=Market.EQUITY_OPTION,
        underlying="NVDA", instrument="NVDA260130C00150000",
        direction=Direction.LONG_CALL, status=Status.OPEN,
        quantity=2, multiplier=100, entry_price=5.0, entry_score=78.0,
        plan=ExitPlan(
            stop_price=3.25, target_price=8.0, trail_activate_at=6.5,
            trail_giveback_pct=0.2, time_stop_ts=NOW,
        ),
        mark_price=5.5, realized_pnl=0.0,
    )
    defaults.update(kw)
    return Position(**defaults)


def _scalp_pos():
    return _pos(
        market=Market.CRYPTO_SPOT, underlying="BTC-USD", instrument="BTC-USD",
        direction=Direction.LONG_SPOT, quantity=0.05, multiplier=1.0,
        entry_price=68000.0,
        plan=ExitPlan(
            stop_price=67000.0, target_price=69800.0,
            trail_activate_at=68800.0, trail_giveback_pct=0.35,
            time_stop_ts=NOW, scalp=True, r_unit=1000.0, vwap_floor=67900.0,
        ),
        mark_price=68500.0,
    )


# ---------------------------------------------------------------- format open
def test_format_open_includes_symbol_and_plan():
    msg = trade_events.format_open(_pos(), fill_price=5.1, score_total=78.0,
                                   liquidity=80, technical=75, sentiment=70)
    assert "Opened NVDA" in msg
    assert "LONG_CALL" in msg
    assert "5.1000" in msg or "5.1" in msg
    assert "stop" in msg
    assert "Score 78.0" in msg


def test_format_open_scalp_tag():
    msg = trade_events.format_open(_scalp_pos(), fill_price=68000.0,
                                   score_total=72.0)
    assert "Scalp" in msg
    assert "R=" in msg


def test_format_open_without_score():
    msg = trade_events.format_open(_pos(), fill_price=5.0)
    assert "Opened NVDA" in msg
    assert "Score" not in msg


# --------------------------------------------------------------- format close
@pytest.mark.parametrize("reason", [
    ExitReason.STOP_LOSS,
    ExitReason.TAKE_PROFIT,
    ExitReason.TRAILING_STOP,
    ExitReason.VWAP_BREAK,
    ExitReason.TIME_STOP,
    ExitReason.MANUAL,
    ExitReason.SESSION_FLATTEN,
])
def test_format_close_all_reasons(reason):
    p = _pos(exit_price=4.0, realized_pnl=-200.0, status=Status.CLOSED)
    msg = trade_events.format_close(p, reason, detail="test detail")
    assert reason.value in msg
    assert "Closed NVDA" in msg
    assert "test detail" in msg


def test_format_close_winner_shows_positive_pnl():
    p = _pos(exit_price=8.0, realized_pnl=600.0, status=Status.CLOSED)
    msg = trade_events.format_close(p, ExitReason.TAKE_PROFIT)
    assert "+600.00" in msg or "+600" in msg


def test_format_close_scalp_includes_r_multiple():
    p = _scalp_pos()
    p.exit_price = 69000.0
    p.realized_pnl = 50.0
    p.status = Status.CLOSED
    msg = trade_events.format_close(p, ExitReason.TAKE_PROFIT)
    assert "R-multiple" in msg


def test_format_close_accepts_string_reason():
    msg = trade_events.format_close(
        _pos(exit_price=5.0, realized_pnl=0.0), "MANUAL"
    )
    assert "MANUAL" in msg


# ------------------------------------------------------------- notify wrappers
def test_notify_open_calls_discord_info():
    with patch.object(trade_events.discord, "info") as info:
        trade_events.notify_open(_pos(), fill_price=5.0)
        assert info.called
        body = info.call_args[0][0]
        assert "Opened NVDA" in body


def test_notify_close_uses_warn_on_loss():
    p = _pos(exit_price=3.0, realized_pnl=-400.0, status=Status.CLOSED)
    with patch.object(trade_events.discord, "send") as send:
        trade_events.notify_close(p, ExitReason.STOP_LOSS, "hit stop")
        assert send.called
        assert send.call_args[0][1] == "WARN"


def test_notify_close_uses_info_on_win():
    p = _pos(exit_price=8.0, realized_pnl=500.0, status=Status.CLOSED)
    with patch.object(trade_events.discord, "send") as send:
        trade_events.notify_close(p, ExitReason.TAKE_PROFIT)
        assert send.call_args[0][1] == "INFO"


def test_notify_open_isolates_discord_failure():
    with patch.object(trade_events.discord, "info", side_effect=RuntimeError("boom")):
        # Must not raise.
        trade_events.notify_open(_pos(), fill_price=5.0)


def test_notify_close_isolates_discord_failure():
    p = _pos(exit_price=5.0, realized_pnl=0.0)
    with patch.object(trade_events.discord, "send", side_effect=RuntimeError("boom")):
        trade_events.notify_close(p, ExitReason.MANUAL)


def test_notify_open_with_assessment():
    a = SymbolAssessment(
        symbol="NVDA", market=Market.EQUITY_OPTION,
        score=ScoreCard(symbol="NVDA", liquidity=80, technical=70, sentiment=75),
        verdict=Verdict.EXECUTE,
    )
    with patch.object(trade_events.discord, "info") as info:
        trade_events.notify_open(_pos(), fill_price=5.0, assessment=a)
        body = info.call_args[0][0]
        assert "L 80" in body
        assert "T 70" in body
        assert "S 75" in body


# ------------------------------------------------------------------- chunking
def test_short_message_is_one_chunk():
    assert discord._chunks("hello") == ["hello"]


def test_multiline_chunks_at_boundary():
    line = "x" * 100
    text = "\n".join([line] * 30)  # 30*100 + newlines > 1900
    chunks = discord._chunks(text)
    assert len(chunks) >= 2
    assert all(len(c) <= discord.MAX_CHUNK for c in chunks)
    # Round-trip content (minus the exact join of newlines at boundaries).
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_long_unbroken_line_is_split_not_truncated():
    """Regression: old _chunks truncated long lines and dropped the tail."""
    payload = "A" * 5000
    chunks = discord._chunks(payload)
    assert len(chunks) >= 3
    assert all(len(c) <= discord.MAX_CHUNK for c in chunks)
    assert "".join(chunks) == payload


def test_long_unbroken_line_with_prefix_buffer():
    prefix = "header\n"
    payload = prefix + ("B" * 4000)
    chunks = discord._chunks(payload)
    assert all(len(c) <= discord.MAX_CHUNK for c in chunks)
    assert "".join(chunks).replace("\n", "") == payload.replace("\n", "")


def test_send_never_raises_without_webhook(monkeypatch):
    class _S:
        discord_webhook = ""
        discord_critical_webhook = ""
        dashboard_url = "http://localhost"
    monkeypatch.setattr(discord, "settings", _S())
    # Should record to tape / log path without raising.
    discord.send("test message", "INFO", channel="test")


def test_send_posts_each_chunk(monkeypatch):
    class _S:
        discord_webhook = "https://example.test/hook"
        discord_critical_webhook = ""
        dashboard_url = "http://localhost"
    monkeypatch.setattr(discord, "settings", _S())
    monkeypatch.setattr(discord, "_throttled", lambda *a, **k: False)
    posts = []

    def fake_post(url, content):
        posts.append(content)

    monkeypatch.setattr(discord, "_post", fake_post)
    monkeypatch.setattr(discord.repo.events, "add", lambda *a, **k: None)
    long = "Z" * 4000
    discord.send(long, "INFO", channel="test", cooldown_s=0)
    assert len(posts) >= 2
