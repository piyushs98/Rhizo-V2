"""
NYSE trading calendar, computed rather than hard-coded.

Post-mortem #6: v1 shipped a literal set of holiday dates that expired on
2027-12-24. A calendar with an expiry date is a scheduled outage. Everything
here is derived from rules, so it is correct for any year.

No third-party dependency. Pure stdlib, fully unit-testable.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache

# NYSE regular session, America/New_York
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
PREMARKET_PREP_OPEN = time(9, 0)


# ------------------------------------------------------------------- helpers
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th `weekday` (Mon=0) of a month. n=1 is the first."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Last `weekday` of a month."""
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lm = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lm) // 451
    month, day = divmod(h + lm - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date, *, shift_saturday_back: bool = True) -> date:
    """US federal observance: Sat -> preceding Fri, Sun -> following Mon."""
    if d.weekday() == 5:  # Saturday
        return d - timedelta(days=1) if shift_saturday_back else d
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


# ------------------------------------------------------------------ calendar
@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset[date]:
    """Full-day NYSE closures for a given calendar year."""
    out: set[date] = set()

    # New Year's Day. NYSE does NOT close the preceding Friday when Jan 1
    # lands on a Saturday - that is the one exception to the observance rule.
    ny = date(year, 1, 1)
    if ny.weekday() != 5:
        out.add(_observed(ny, shift_saturday_back=False))

    out.add(_nth_weekday(year, 1, 0, 3))                 # MLK Jr Day
    out.add(_nth_weekday(year, 2, 0, 3))                 # Washington's Birthday
    out.add(easter_sunday(year) - timedelta(days=2))     # Good Friday
    out.add(_last_weekday(year, 5, 0))                   # Memorial Day

    if year >= 2022:                                     # Juneteenth
        out.add(_observed(date(year, 6, 19)))

    out.add(_observed(date(year, 7, 4)))                 # Independence Day
    out.add(_nth_weekday(year, 9, 0, 1))                 # Labor Day
    out.add(_nth_weekday(year, 11, 3, 4))                # Thanksgiving
    out.add(_observed(date(year, 12, 25)))               # Christmas

    return frozenset(out)


@lru_cache(maxsize=64)
def early_closes(year: int) -> frozenset[date]:
    """Sessions ending at 13:00 ET."""
    out: set[date] = set()

    # Day after Thanksgiving
    out.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))

    # July 3, when it is a weekday and the 4th is the observed holiday
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5 and date(year, 7, 4).weekday() < 5:
        out.add(jul3)

    # Christmas Eve, when it is a weekday and Christmas itself is on a weekday
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and date(year, 12, 25).weekday() < 5:
        out.add(dec24)

    return frozenset(out - holidays(year))


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def session_close(d: date) -> time:
    return EARLY_CLOSE if d in early_closes(d.year) else MARKET_CLOSE


def is_market_open(dt: datetime) -> bool:
    """`dt` must already be in America/New_York."""
    if not is_trading_day(dt.date()):
        return False
    return MARKET_OPEN <= dt.time() < session_close(dt.date())


def next_trading_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def next_open(dt: datetime) -> datetime:
    """Next regular-session open at or after `dt` (naive ET in, naive ET out)."""
    d = dt.date()
    if is_trading_day(d) and dt.time() < MARKET_OPEN:
        return datetime.combine(d, MARKET_OPEN)
    return datetime.combine(next_trading_day(d), MARKET_OPEN)


def next_close(dt: datetime) -> datetime:
    d = dt.date()
    if is_trading_day(d) and dt.time() < session_close(d):
        return datetime.combine(d, session_close(d))
    nxt = next_trading_day(d)
    return datetime.combine(nxt, session_close(nxt))
