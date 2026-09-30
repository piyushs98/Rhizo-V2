#!/usr/bin/env python3
"""Wipe old trades and restore the paper account to STARTING_CAPITAL."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import repositories as repo
from app.db.connection import init_db, query_one


def main() -> int:
    init_db()
    capital = float(settings.starting_capital)
    before = repo.ledger.get()
    n_pos = query_one("SELECT COUNT(*) n FROM positions")
    print(
        f"Before: cash {before.get('cash', 0):,.2f} · "
        f"{before.get('trades_opened', 0)} opened · "
        f"{int(n_pos['n']) if n_pos else 0} positions"
    )
    led = repo.ledger.reset_book(capital)
    if repo.kv.get("force_regime", "").upper() == "CRYPTO":
        repo.kv.set("force_regime", "")
    repo.events.add(
        "INFO",
        "engine",
        f"Book reset to {capital:,.0f}. Equity desk only.",
    )
    print(
        f"After:  cash {led['cash']:,.2f} · "
        f"{led['trades_opened']} opened · starting {led['starting_capital']:,.2f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
