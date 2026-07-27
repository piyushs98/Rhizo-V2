"""
Shared HTTP session.

Post-mortem #2: Yahoo returned "Too Many Requests" on Render because a
datacentre IP plus a default python-requests fingerprint looks exactly like
what it is. A browser UA, connection reuse, and deliberate pacing fixed it.
The adapter also guarantees a timeout on every request, so a call can never
be issued without one by accident.
"""
from __future__ import annotations

import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.config import settings

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


class TimeoutAdapter(HTTPAdapter):
    """Injects a default timeout when the caller forgot one."""

    def __init__(self, *a, timeout: float = 15.0, **kw):
        self._timeout = timeout
        super().__init__(*a, **kw)

    def send(self, request, **kw):
        if kw.get("timeout") is None:
            kw["timeout"] = self._timeout
        return super().send(request, **kw)


def _build() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    adapter = TimeoutAdapter(
        timeout=settings.http_timeout_s,
        pool_connections=10,
        pool_maxsize=20,
        max_retries=Retry(
            total=2, backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        ),
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _build()

_pace_lock = threading.Lock()
_last_call: dict[str, float] = {}


def pace(key: str, min_interval_s: float = 2.0) -> None:
    """Space out calls to a given host. Cheap insurance against rate limits."""
    with _pace_lock:
        now = time.time()
        gap = now - _last_call.get(key, 0.0)
        if gap < min_interval_s:
            time.sleep(min_interval_s - gap)
        _last_call[key] = time.time()
