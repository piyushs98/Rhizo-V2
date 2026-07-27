"""The calendar must be correct for years nobody hard-coded."""
from datetime import date

from app import calendar_nyse as cal


def test_easter_known_years():
    assert cal.easter_sunday(2026) == date(2026, 4, 5)
    assert cal.easter_sunday(2027) == date(2027, 3, 28)
    assert cal.easter_sunday(2030) == date(2030, 4, 21)


def test_good_friday_is_a_holiday():
    assert date(2026, 4, 3) in cal.holidays(2026)


def test_2026_holidays():
    h = cal.holidays(2026)
    assert date(2026, 1, 1) in h        # New Year's, a Thursday
    assert date(2026, 1, 19) in h       # MLK
    assert date(2026, 2, 16) in h       # Washington
    assert date(2026, 5, 25) in h       # Memorial
    assert date(2026, 6, 19) in h       # Juneteenth
    assert date(2026, 7, 3) in h        # Jul 4 is a Saturday -> observed Friday
    assert date(2026, 9, 7) in h        # Labor
    assert date(2026, 11, 26) in h      # Thanksgiving
    assert date(2026, 12, 25) in h      # Christmas


def test_new_year_on_saturday_is_not_observed():
    """NYSE stays open the preceding Friday. The one exception to the rule."""
    assert date(2022, 1, 1).weekday() == 5
    assert date(2021, 12, 31) not in cal.holidays(2021)


def test_calendar_extends_past_2027():
    """v1's hard-coded set expired on 2027-12-24. This one cannot."""
    for year in (2028, 2035, 2040):
        h = cal.holidays(year)
        assert len(h) >= 9
        assert any(d.month == 7 for d in h)


def test_trading_days():
    assert cal.is_trading_day(date(2026, 7, 24))       # Friday
    assert not cal.is_trading_day(date(2026, 7, 25))   # Saturday
    assert not cal.is_trading_day(date(2026, 7, 3))    # observed holiday


def test_early_closes():
    assert date(2026, 11, 27) in cal.early_closes(2026)   # day after Thanksgiving
    assert cal.session_close(date(2026, 11, 27)) == cal.EARLY_CLOSE
    assert cal.session_close(date(2026, 7, 24)) == cal.MARKET_CLOSE


def test_next_trading_day_skips_the_weekend():
    assert cal.next_trading_day(date(2026, 7, 24)) == date(2026, 7, 27)
