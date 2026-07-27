"""
Wall-clock envelopes for anything that touches a network.

Post-mortem #10 and #18: v1 hung on Yahoo and Gemini sockets with no ceiling,
which eventually got the whole worker killed. Every external call in this
system runs inside `budget()`. If it overruns, the caller gets a
`CallTimeout` and moves on. The hung thread is abandoned, not awaited.
"""
from __future__ import annotations

import concurrent.futures as _cf
import functools
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_POOL = _cf.ThreadPoolExecutor(max_workers=12, thread_name_prefix="budget")


class CallTimeout(TimeoutError):
    def __init__(self, label: str, seconds: float):
        super().__init__(f"{label} exceeded its {seconds:.0f}s budget")
        self.label = label
        self.seconds = seconds


def budget(label: str, seconds: float, fn: Callable[..., T], *a: Any, **kw: Any) -> T:
    """Run `fn` with a hard wall-clock ceiling."""
    fut = _POOL.submit(fn, *a, **kw)
    try:
        return fut.result(timeout=seconds)
    except _cf.TimeoutError as exc:
        fut.cancel()
        raise CallTimeout(label, seconds) from exc


def with_budget(label: str, seconds: float):
    """Decorator form."""
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*a: Any, **kw: Any) -> T:
            return budget(label, seconds, fn, *a, **kw)
        return wrapper
    return deco


def retry(times: int = 3, delay: float = 1.0, backoff: float = 2.0,
          exceptions: tuple = (Exception,)):
    """Simple bounded retry. Never used on anything that places an order."""
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*a: Any, **kw: Any) -> T:
            wait, last = delay, None
            for attempt in range(times):
                try:
                    return fn(*a, **kw)
                except exceptions as exc:
                    last = exc
                    if attempt == times - 1:
                        break
                    time.sleep(wait)
                    wait *= backoff
            raise last  # type: ignore[misc]
        return wrapper
    return deco
