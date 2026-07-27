"""
Structured logging. JSON to stdout (for Render / any log sink), plain text
to a rotating file for local reading.

Every log line carries the component so a single grep separates the engine
from the web process.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("symbol", "scan_id", "position_id", "regime", "component"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup(component: str = "app") -> logging.Logger:
    global _CONFIGURED
    root = logging.getLogger()
    if not _CONFIGURED:
        root.setLevel(getattr(logging, settings.log_level, logging.INFO))
        for h in list(root.handlers):
            root.removeHandler(h)

        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(
            JsonFormatter() if settings.log_json
            else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        )
        root.addHandler(stream)

        try:
            Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                Path(settings.log_dir) / f"{component}.log",
                maxBytes=5_000_000, backupCount=3,
            )
            fh.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
            )
            root.addHandler(fh)
        except OSError:
            pass  # read-only filesystem: stdout is enough

        for noisy in ("urllib3", "yfinance", "peewee", "httpx", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        _CONFIGURED = True

    return logging.getLogger(component)
