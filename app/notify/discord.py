"""
Discord alerting.

Three rules carried over from v1, all learned the hard way:
  - chunk at 1900 chars (post-mortem #5: HTTP 400 on >2000)
  - never raise into the caller; an alerting failure must not stop a trade
  - rate-limit per key, so one repeating error cannot emit 500 messages

New in v2: severity tiers, and an optional separate webhook for CRITICAL so
the noise channel and the wake-me-up channel are different.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Literal

import requests

from app.config import settings
from app.db import repositories as repo

log = logging.getLogger("notify")

Level = Literal["INFO", "WARN", "CRITICAL"]

MAX_CHUNK = 1900
_ICON = {"INFO": "\u25cf", "WARN": "\u25b2", "CRITICAL": "\u2716"}

_last_sent: dict[str, float] = {}
_lock = threading.Lock()
DEFAULT_COOLDOWN_S = 300.0


def _chunks(text: str) -> list[str]:
    if len(text) <= MAX_CHUNK:
        return [text]
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > MAX_CHUNK:
            if buf:
                out.append(buf)
            buf = line[:MAX_CHUNK]
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


def _post(url: str, content: str) -> None:
    for attempt in range(3):
        try:
            r = requests.post(url, json={"content": content}, timeout=10)
            if r.status_code == 429:
                wait = float(r.json().get("retry_after", 2))
                time.sleep(min(wait, 10))
                continue
            if r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                log.warning("discord rejected message: %s %s",
                            r.status_code, r.text[:200])
            return
        except requests.RequestException as exc:
            log.warning("discord post failed (%s/3): %s", attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))


def _throttled(key: str, cooldown: float) -> bool:
    with _lock:
        now = time.time()
        last = _last_sent.get(key, 0.0)
        if now - last < cooldown:
            return True
        _last_sent[key] = now
        return False


def send(
    message: str,
    level: Level = "INFO",
    *,
    channel: str = "engine",
    dedupe_key: str | None = None,
    cooldown_s: float = DEFAULT_COOLDOWN_S,
    meta: dict | None = None,
) -> None:
    """Emit an alert. Always records to the event tape, even with no webhook."""
    try:
        repo.events.add(level, channel, message, meta)
    except Exception:
        log.exception("could not write event to tape")

    log.log(
        {"INFO": logging.INFO, "WARN": logging.WARNING,
         "CRITICAL": logging.ERROR}[level],
        "[%s] %s", channel, message,
    )

    if dedupe_key and _throttled(dedupe_key, cooldown_s):
        return

    url = settings.discord_webhook
    if level == "CRITICAL" and settings.discord_critical_webhook:
        url = settings.discord_critical_webhook
    if not url:
        return

    header = f"{_ICON[level]} **{level}** \u00b7 `{channel}`"
    body = f"{header}\n{message}"
    if level == "CRITICAL":
        body += f"\n{settings.dashboard_url}"

    try:
        for chunk in _chunks(body):
            _post(url, chunk)
    except Exception:
        log.exception("alerting failed; continuing")


def info(msg: str, **kw) -> None:
    send(msg, "INFO", **kw)


def warn(msg: str, **kw) -> None:
    send(msg, "WARN", **kw)


def critical(msg: str, **kw) -> None:
    send(msg, "CRITICAL", **kw)
