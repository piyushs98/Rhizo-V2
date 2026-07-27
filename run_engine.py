#!/usr/bin/env python3
"""
Trading engine entrypoint. No web server, no port binding.

Post-mortem #1: in v1 the trading module called `keep_alive()` and fought the
web app for $PORT. Nothing in this process opens a socket to listen on.
"""
from __future__ import annotations

import sys

from app.config import ConfigError, assert_valid, settings
from app.db.connection import init_db
from app.engine.scheduler import Engine
from app.logging_setup import setup
from app.resilience.singleton import AlreadyRunning, SingleInstance


def main() -> int:
    log = setup("engine")
    try:
        assert_valid()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    lock = SingleInstance("engine", directory=settings.log_dir)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        log.error("Refusing to start a second engine: %s", exc)
        return 3

    init_db()
    log.info(
        "engine starting | env=%s | equity=%s | crypto=%s | dry_run=%s",
        settings.env, settings.equity_enabled, settings.crypto_enabled,
        settings.dry_run,
    )
    try:
        Engine().run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
