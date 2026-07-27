#!/usr/bin/env python3
"""Dashboard entrypoint. Reads the database; never trades."""
from __future__ import annotations

import sys

import uvicorn

from app.config import ConfigError, assert_valid, settings
from app.logging_setup import setup


def main() -> int:
    log = setup("web")
    try:
        assert_valid()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    uvicorn.run(
        "app.web.server:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
