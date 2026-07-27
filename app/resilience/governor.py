"""
Rolling-window rate ceiling. Hard, not advisory.

Keeps a 60-second window of request timestamps. When full, later callers
either block until a slot frees or raise — they cannot exceed the budget by
accident. A 429 from the venue fills the window immediately: if their count
disagrees with ours, ours is wrong and we back off hard.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque


class RateLimitExceeded(RuntimeError):
    """Raised when the hard ceiling is full and non-blocking acquire was used."""


@dataclass
class GovernorSnapshot:
    name: str
    used: int
    limit: int
    window_s: float
    remaining: int
    oldest_age_s: float | None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "used": self.used,
            "limit": self.limit,
            "window_s": self.window_s,
            "remaining": self.remaining,
            "oldest_age_s": self.oldest_age_s,
            "util_pct": round(100.0 * self.used / self.limit, 1) if self.limit else 0.0,
        }


class RateGovernor:
    def __init__(
        self,
        name: str,
        *,
        limit: int = 100,
        window_s: float = 60.0,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self.name = name
        self.limit = limit
        self.window_s = window_s
        self._hits: Deque[float] = deque()
        self._lock = threading.Lock()
        self._total = 0
        self._blocked = 0
        self._rate_limited = 0

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()

    def used(self) -> int:
        with self._lock:
            self._prune(time.monotonic())
            return len(self._hits)

    def remaining(self) -> int:
        return max(0, self.limit - self.used())

    def acquire(self, *, block: bool = True, timeout: float | None = None) -> None:
        """
        Reserve one request slot.

        block=True waits until a slot frees (up to timeout seconds if set).
        block=False raises RateLimitExceeded immediately when full.
        """
        deadline = None if timeout is None else (time.monotonic() + timeout)
        while True:
            with self._lock:
                now = time.monotonic()
                self._prune(now)
                if len(self._hits) < self.limit:
                    self._hits.append(now)
                    self._total += 1
                    return
                self._blocked += 1
                if not block:
                    raise RateLimitExceeded(
                        f"{self.name}: {self.limit}/{self.window_s:.0f}s budget full"
                    )
                # Sleep until the oldest hit ages out of the window.
                wait = max(0.01, self._hits[0] + self.window_s - now)
            if deadline is not None and time.monotonic() + wait > deadline:
                raise RateLimitExceeded(
                    f"{self.name}: timed out waiting for a request slot"
                )
            time.sleep(min(wait, 0.25))

    def record_429(self) -> None:
        """
        Venue said we are over. Fill the window so the next acquire blocks
        for a full period — their count disagreeing with ours means ours is wrong.
        """
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._rate_limited += 1
            while len(self._hits) < self.limit:
                self._hits.append(now)

    def snapshot(self) -> GovernorSnapshot:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            used = len(self._hits)
            oldest = (now - self._hits[0]) if self._hits else None
            return GovernorSnapshot(
                name=self.name,
                used=used,
                limit=self.limit,
                window_s=self.window_s,
                remaining=max(0, self.limit - used),
                oldest_age_s=round(oldest, 2) if oldest is not None else None,
            )

    def stats(self) -> dict:
        snap = self.snapshot()
        d = snap.to_dict()
        d["total_acquired"] = self._total
        d["blocked"] = self._blocked
        d["http_429s"] = self._rate_limited
        return d

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# Process-wide governors. Tests may call reset_all().
_alpaca: RateGovernor | None = None
_lock = threading.Lock()


def alpaca_governor() -> RateGovernor:
    """Soft self-imposed ceiling (default 100/min). Below Alpaca's 200/min."""
    global _alpaca
    with _lock:
        if _alpaca is None:
            from app.config import settings
            _alpaca = RateGovernor(
                "alpaca",
                limit=settings.alpaca_rate_limit_per_min,
                window_s=60.0,
            )
        return _alpaca


def reset_all() -> None:
    global _alpaca
    with _lock:
        if _alpaca is not None:
            _alpaca.reset()
        _alpaca = None


def budget_status() -> dict:
    """Dashboard-facing snapshot of every governor."""
    g = alpaca_governor()
    return {
        "alpaca": g.stats(),
        "soft_limit_per_min": g.limit,
        "hard_venue_limit_per_min": 200,
    }
