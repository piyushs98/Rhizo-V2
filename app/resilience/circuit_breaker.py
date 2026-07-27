"""
Circuit breaker, one instance per external dependency.

Fail-closed: after N consecutive failures the breaker opens and every call is
short-circuited for the cooldown window. This is what stops a Yahoo outage
from turning into a thousand timed-out requests and a scan that never ends.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class BreakerOpen(RuntimeError):
    def __init__(self, name: str, retry_in: float):
        super().__init__(f"{name} circuit is open; retry in {retry_in:.0f}s")
        self.name = name
        self.retry_in = retry_in


@dataclass
class CircuitBreaker:
    name: str
    threshold: int = 5
    cooldown_s: float = 900.0
    failures: int = 0
    opened_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self.opened_at is None:
                return False
            if time.time() - self.opened_at >= self.cooldown_s:
                self.opened_at = None
                self.failures = 0
                return False
            return True

    def retry_in(self) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.cooldown_s - (time.time() - self.opened_at))

    def guard(self) -> None:
        if self.is_open:
            raise BreakerOpen(self.name, self.retry_in())

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold and self.opened_at is None:
                self.opened_at = time.time()

    def state(self) -> dict:
        return {
            "name": self.name,
            "open": self.is_open,
            "failures": self.failures,
            "retry_in": round(self.retry_in(), 1),
        }


_REGISTRY: dict[str, CircuitBreaker] = {}
_REG_LOCK = threading.Lock()


def get_breaker(name: str, threshold: int = 5, cooldown_s: float = 900.0) -> CircuitBreaker:
    with _REG_LOCK:
        if name not in _REGISTRY:
            _REGISTRY[name] = CircuitBreaker(name, threshold, cooldown_s)
        return _REGISTRY[name]


def all_states() -> list[dict]:
    return [b.state() for b in _REGISTRY.values()]
