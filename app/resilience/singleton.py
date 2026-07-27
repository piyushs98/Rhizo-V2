"""
Single-instance guard for the trading engine.

Post-mortem #12: v1 double-spawned bots when two gthreads raced through the
boot path, and the mitigation was an in-process lock - which does nothing
against a second OS process. This is an OS-level advisory file lock. Two
engines cannot run against the same database, whatever launches them.
"""
from __future__ import annotations

import atexit
import os
from pathlib import Path

try:
    import fcntl
    _HAVE_FCNTL = True
except ImportError:      # Windows
    _HAVE_FCNTL = False
    import msvcrt        # type: ignore


class AlreadyRunning(RuntimeError):
    pass


class SingleInstance:
    def __init__(self, name: str, directory: str | Path = "data"):
        self.path = Path(directory) / f"{name}.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None

    def acquire(self) -> None:
        self._fh = open(self.path, "a+")
        try:
            if _HAVE_FCNTL:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            holder = ""
            try:
                holder = self.path.read_text().strip()
            except Exception:
                pass
            raise AlreadyRunning(
                f"Another instance holds {self.path}"
                + (f" (pid {holder})" if holder else "")
            ) from exc

        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        atexit.register(self.release)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if _HAVE_FCNTL:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
        except Exception:
            pass
        finally:
            self._fh = None

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
