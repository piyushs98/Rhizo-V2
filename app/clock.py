"""
The session router. This is the spine of the whole system.

The desk runs two shifts:

    09:00-09:30 ET  PREP    warm caches, build the morning read
    09:30-16:00 ET  EQUITY  options on the equity universe
    16:00-09:00 ET  CRYPTO  spot crypto through the night
    weekends/holidays       CRYPTO all day

Everything downstream - which universe to scan, which market adapter to use,
which exit rules apply - is a pure function of the regime returned here.
No component computes market hours for itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from app import calendar_nyse as cal

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

PREP_START = cal.PREMARKET_PREP_OPEN  # 09:00


class Regime(str, Enum):
    PREP = "PREP"
    EQUITY = "EQUITY"
    CRYPTO = "CRYPTO"
    IDLE = "IDLE"


@dataclass(frozen=True)
class SessionState:
    regime: Regime
    now_et: datetime
    session_date: date          # the ET calendar date this cycle belongs to
    trading_day: bool           # is today an NYSE session day
    early_close: bool
    next_handoff_et: datetime   # when the regime changes next
    label: str                  # human string for the dashboard

    @property
    def seconds_to_handoff(self) -> int:
        return max(0, int((self.next_handoff_et - self.now_et).total_seconds()))

    def to_dict(self) -> dict:
        return {
            "regime": self.regime.value,
            "now_et": self.now_et.isoformat(),
            "session_date": self.session_date.isoformat(),
            "trading_day": self.trading_day,
            "early_close": self.early_close,
            "next_handoff_et": self.next_handoff_et.isoformat(),
            "seconds_to_handoff": self.seconds_to_handoff,
            "label": self.label,
        }


def now_et() -> datetime:
    return datetime.now(tz=ET)


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


def _aware(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=ET)


def resolve(
    when: datetime | None = None,
    *,
    equity_enabled: bool = True,
    crypto_enabled: bool = True,
    force: str = "",
) -> SessionState:
    """Determine the current regime and when it next changes."""
    dt = (when or now_et()).astimezone(ET)
    today = dt.date()
    trading_day = cal.is_trading_day(today)
    early = today in cal.early_closes(today.year)
    close_t = cal.session_close(today)

    # --- forced override (dashboard control or FORCE_REGIME env)
    forced = (force or "").upper()
    if forced in {"EQUITY", "CRYPTO", "IDLE"}:
        return SessionState(
            regime=Regime(forced),
            now_et=dt,
            session_date=today,
            trading_day=trading_day,
            early_close=early,
            next_handoff_et=dt + timedelta(hours=1),
            label=f"{forced.title()} desk (forced)",
        )

    # --- natural regime
    if trading_day and PREP_START <= dt.time() < cal.MARKET_OPEN:
        regime = Regime.PREP
        handoff = _aware(today, cal.MARKET_OPEN)
        label = "Pre-market prep"
    elif trading_day and cal.MARKET_OPEN <= dt.time() < close_t:
        regime = Regime.EQUITY
        handoff = _aware(today, close_t)
        label = "Equity options desk" + (" (early close)" if early else "")
    else:
        regime = Regime.CRYPTO
        handoff = _next_prep_start(dt)
        label = "Crypto desk (overnight)"

    # --- apply master switches. A disabled desk falls back to the other one.
    if regime in (Regime.EQUITY, Regime.PREP) and not equity_enabled:
        regime = Regime.CRYPTO if crypto_enabled else Regime.IDLE
        label = "Crypto desk (equities off)" if crypto_enabled else "Idle"
    elif regime is Regime.CRYPTO and not crypto_enabled:
        regime = Regime.IDLE
        label = "Idle (crypto off, market closed)"

    return SessionState(
        regime=regime,
        now_et=dt,
        session_date=today,
        trading_day=trading_day,
        early_close=early,
        next_handoff_et=handoff,
        label=label,
    )


def _next_prep_start(dt: datetime) -> datetime:
    """When the crypto shift hands the book back to the equity desk."""
    today = dt.date()
    if cal.is_trading_day(today) and dt.time() < PREP_START:
        return _aware(today, PREP_START)
    return _aware(cal.next_trading_day(today), PREP_START)


def session_key(state: SessionState) -> str:
    """
    Stable identifier for "this shift". Used as part of the trade
    idempotency key so one signal cannot fire twice in the same session.

    Equity sessions key on the calendar date. Crypto sessions key on the
    date of the *overnight block* they belong to, so 23:00 Monday and
    02:00 Tuesday are the same shift.
    """
    if state.regime in (Regime.EQUITY, Regime.PREP):
        return f"EQ-{state.session_date.isoformat()}"
    anchor = state.now_et
    if anchor.time() < PREP_START:
        anchor = anchor - timedelta(days=1)
    return f"CX-{anchor.date().isoformat()}"


def day_boundaries_et(d: date) -> tuple[datetime, datetime]:
    """[00:00, 24:00) in ET for a given date. Used by the session ribbon."""
    start = _aware(d, time(0, 0))
    return start, start + timedelta(days=1)


def ribbon_segments(d: date) -> list[dict]:
    """
    Describe a 24h ET day as coloured territories for the dashboard ribbon.
    Fractions are 0..1 across the day, so the UI does no date maths.
    """
    trading = cal.is_trading_day(d)
    close_t = cal.session_close(d)

    def frac(t: time) -> float:
        return (t.hour * 3600 + t.minute * 60 + t.second) / 86400.0

    if not trading:
        return [{"regime": "CRYPTO", "start": 0.0, "end": 1.0,
                 "label": "Crypto — market closed"}]

    return [
        {"regime": "CRYPTO", "start": 0.0, "end": frac(PREP_START),
         "label": "Crypto — overnight"},
        {"regime": "PREP", "start": frac(PREP_START), "end": frac(cal.MARKET_OPEN),
         "label": "Prep"},
        {"regime": "EQUITY", "start": frac(cal.MARKET_OPEN), "end": frac(close_t),
         "label": "Equity options"},
        {"regime": "CRYPTO", "start": frac(close_t), "end": 1.0,
         "label": "Crypto — after the bell"},
    ]
