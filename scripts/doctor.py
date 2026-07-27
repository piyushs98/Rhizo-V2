#!/usr/bin/env python3
"""
Preflight check. Run this before every deploy and after every config change.

It answers the question v1 could never answer without waiting for 09:30:
is this thing actually going to work today?

    python scripts/doctor.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "
issues = 0


def check(label: str, status: str, detail: str = "") -> None:
    global issues
    if status == FAIL:
        issues += 1
    print(f"[{status}] {label}" + (f"  —  {detail}" if detail else ""))


def main() -> int:
    print("\nJanus Desk preflight\n" + "─" * 62)

    # ---- configuration
    from app.config import settings
    errs = settings.validate()
    if errs:
        for e in errs:
            check("configuration", FAIL, e)
    else:
        check("configuration", OK, f"env={settings.env}")

    # ---- database
    try:
        from app.db.connection import init_db, query_one
        init_db()
        row = query_one("SELECT COUNT(*) n FROM positions")
        check("database", OK, f"{settings.db_path} ({row['n']} positions)")
    except Exception as exc:
        check("database", FAIL, str(exc))
        return 1

    # ---- persistence warning
    db = Path(settings.db_path).resolve()
    if settings.env == "production" and not str(db).startswith("/var/data"):
        check("persistence", WARN,
              "the database is not on a mounted disk; a deploy will wipe it")
    else:
        check("persistence", OK, str(db.parent))

    # ---- calendar
    from datetime import date
    from app import calendar_nyse as cal
    from app.clock import resolve
    y = date.today().year
    check("calendar", OK,
          f"{len(cal.holidays(y))} holidays in {y}, "
          f"{len(cal.holidays(y + 10))} in {y + 10}")

    state = resolve(equity_enabled=settings.equity_enabled,
                    crypto_enabled=settings.crypto_enabled,
                    force=settings.force_regime)
    check("session router", OK,
          f"{state.regime.value} — {state.label}, "
          f"handoff at {state.next_handoff_et:%H:%M} ET")

    # ---- single instance
    from app.resilience.singleton import AlreadyRunning, SingleInstance
    lock = SingleInstance("engine", directory=settings.log_dir)
    try:
        lock.acquire()
        lock.release()
        check("engine lock", OK, "no other engine is running")
    except AlreadyRunning as exc:
        check("engine lock", WARN, str(exc))

    # ---- data feeds
    from app.data.providers import DataUnavailable, crypto_provider, equity_provider
    provider_name = settings.market_data_provider
    eq = equity_provider()
    batch = hasattr(eq, "quotes_many") and hasattr(eq, "bars_many")
    check("market data provider", OK,
          f"{provider_name} · batch={'yes' if batch else 'no'} · "
          f"name={getattr(eq, 'name', '?')}")

    try:
        q = crypto_provider().quote("BTC-USD")
        check("crypto data", OK,
              f"BTC-USD {q.price:,.2f} via {q.meta.get('venue', '?')}")
    except DataUnavailable as exc:
        check("crypto data", FAIL, str(exc)[:120])
    except Exception as exc:
        check("crypto data", FAIL, f"{type(exc).__name__}: {exc}"[:120])

    try:
        q = equity_provider().quote("SPY")
        check("equity data", OK, f"SPY {q.price:,.2f}")
    except Exception as exc:
        check("equity data", WARN, f"{type(exc).__name__}: {str(exc)[:100]}")

    # ---- request budget
    from app.resilience import governor as gov
    st = gov.budget_status()
    a = st.get("alpaca", {})
    check("request budget", OK,
          f"{a.get('used', 0)}/{a.get('limit', '?')} in window · "
          f"soft {st.get('soft_limit_per_min')}/min · "
          f"venue hard {st.get('hard_venue_limit_per_min')}/min")

    # ---- sentiment freshness
    from app.agents import news as news_agent
    nst = news_agent.status()
    for key in ("macro", "crypto"):
        row = nst[key]
        age = row.get("age_seconds")
        age_s = f"{age:.0f}s" if age is not None else "never"
        check(f"sentiment {key}",
              OK if row.get("fresh") else WARN,
              f"bias={row.get('bias', 0):+.2f} · age={age_s}"
              + (" · STALE" if row.get("stale") else ""))

    # ---- optional services
    check("LLM commentary",
          OK if settings.llm_available else WARN,
          "advisory only" if settings.llm_available
          else "no keys set; the desk trades normally without it")

    check("Discord alerts",
          OK if settings.discord_available else WARN,
          "configured" if settings.discord_available
          else "no webhook; alerts go to the tape and stdout only")

    # ---- risk posture
    from app.engine import risk
    s = risk.portfolio_summary()
    check("book", OK,
          f"equity {s['equity']:,.2f} · {s['open_count']} open · "
          + ("HALTED" if s["halted"] else "trading enabled"))

    print("─" * 62)
    if issues:
        print(f"\n{issues} blocking problem(s). Fix these before starting.\n")
        return 1
    print("\nReady.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
