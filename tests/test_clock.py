"""The two-shift router. Everything downstream depends on this being right."""
from datetime import datetime

from app.clock import ET, Regime, resolve, ribbon_segments, session_key


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_equity_shift_during_the_session():
    assert resolve(at(2026, 7, 24, 10, 30)).regime is Regime.EQUITY


def test_prep_window_before_the_open():
    assert resolve(at(2026, 7, 24, 9, 15)).regime is Regime.PREP


def test_crypto_takes_the_book_after_the_bell():
    assert resolve(at(2026, 7, 24, 16, 1)).regime is Regime.CRYPTO
    assert resolve(at(2026, 7, 24, 23, 30)).regime is Regime.CRYPTO
    assert resolve(at(2026, 7, 25, 3, 0)).regime is Regime.CRYPTO


def test_crypto_runs_all_weekend():
    assert resolve(at(2026, 7, 25, 12, 0)).regime is Regime.CRYPTO   # Saturday
    assert resolve(at(2026, 7, 26, 12, 0)).regime is Regime.CRYPTO   # Sunday


def test_crypto_runs_on_market_holidays():
    assert resolve(at(2026, 12, 25, 11, 0)).regime is Regime.CRYPTO


def test_early_close_hands_over_at_1300():
    state = resolve(at(2026, 11, 27, 13, 30))
    assert state.regime is Regime.CRYPTO
    assert resolve(at(2026, 11, 27, 12, 30)).regime is Regime.EQUITY


def test_handoff_time_is_the_bell():
    state = resolve(at(2026, 7, 24, 10, 0))
    assert state.next_handoff_et.hour == 16
    assert state.seconds_to_handoff == 6 * 3600


def test_disabling_a_desk_falls_through_to_the_other():
    assert resolve(at(2026, 7, 24, 10, 30), equity_enabled=False).regime is Regime.CRYPTO
    assert resolve(at(2026, 7, 24, 22, 0), crypto_enabled=False).regime is Regime.IDLE


def test_force_override_wins():
    assert resolve(at(2026, 7, 25, 12, 0), force="EQUITY").regime is Regime.EQUITY


def test_overnight_block_is_one_session_key():
    """23:00 Monday and 02:00 Tuesday are the same crypto shift."""
    a = session_key(resolve(at(2026, 7, 20, 23, 0)))
    b = session_key(resolve(at(2026, 7, 21, 2, 0)))
    assert a == b == "CX-2026-07-20"


def test_equity_session_key_is_the_date():
    assert session_key(resolve(at(2026, 7, 24, 10, 0))) == "EQ-2026-07-24"


def test_ribbon_covers_the_whole_day():
    from datetime import date
    segs = ribbon_segments(date(2026, 7, 24))
    assert segs[0]["start"] == 0.0
    assert segs[-1]["end"] == 1.0
    for a, b in zip(segs, segs[1:]):
        assert abs(a["end"] - b["start"]) < 1e-9


def test_ribbon_on_a_closed_day_is_all_crypto():
    from datetime import date
    segs = ribbon_segments(date(2026, 7, 25))
    assert len(segs) == 1 and segs[0]["regime"] == "CRYPTO"
