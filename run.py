#!/usr/bin/env python3
"""
Supervisor. Runs the engine and the dashboard as two child processes and
restarts either one if it dies.

This exists so a single Render service (or a single `python run.py` locally)
gives you process isolation without paying for two services. The two children
share one WAL-mode SQLite file and never share memory, so neither can corrupt
the other's state.

If you later split them into separate services, delete nothing - just point
one service at `run_engine.py` and the other at `run_web.py`, and give them a
shared Postgres instead of the file. No application code changes.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
RESTART_DELAY_S = 5
CHILDREN = {
    "engine": [sys.executable, "-u", str(ROOT / "run_engine.py")],
    "web": [sys.executable, "-u", str(ROOT / "run_web.py")],
}

procs: dict[str, subprocess.Popen] = {}
shutting_down = False


def log(msg: str) -> None:
    print(f"[supervisor] {msg}", flush=True)


def spawn(name: str) -> None:
    log(f"starting {name}")
    procs[name] = subprocess.Popen(
        CHILDREN[name], cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def stop_all(signum=None, _frame=None) -> None:
    global shutting_down
    shutting_down = True
    log(f"shutting down (signal {signum})")
    for name, p in procs.items():
        if p.poll() is None:
            log(f"stopping {name} (pid {p.pid})")
            p.terminate()
    deadline = time.time() + 20
    for name, p in procs.items():
        remaining = max(0.5, deadline - time.time())
        try:
            p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            log(f"{name} did not stop; killing")
            p.kill()


def main() -> int:
    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    for name in CHILDREN:
        spawn(name)

    while not shutting_down:
        time.sleep(2)
        for name, p in list(procs.items()):
            code = p.poll()
            if code is None:
                continue
            if shutting_down:
                break
            log(f"{name} exited with code {code}; restarting in {RESTART_DELAY_S}s")
            time.sleep(RESTART_DELAY_S)
            spawn(name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
