"""
The engine loop.

One thread, one tick, no nested state machines. Every tick does the same
four things in the same order:

    1. process any commands the dashboard queued
    2. manage open positions      <- always, before anything else
    3. scan, if the current shift is due for one
    4. heartbeat, then sleep

Managing comes before scanning on purpose. If the process is short on time,
budget, or data, the thing that must still happen is honouring the stops on
money already at risk.

The immortal contract from v1 is preserved and tightened: the loop body is
wrapped, a failure logs, alerts, backs off, and continues. It never exits on
a cycle error. What is new is that the failure is also recorded to the event
tape, and repeated failures escalate rather than repeating identically
forever.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone

from app import clock
from app.broker.base import Broker
from app.broker.paper import PaperBroker
from app.clock import Regime, SessionState
from app.config import settings
from app.db import repositories as repo
from app.domain.models import ExitReason
from app.engine import position_manager, risk, scanner
from app.markets.adapters import adapter_for_regime
from app.notify import discord

log = logging.getLogger("engine")

COMPONENT = "engine"
BACKOFF_S = 60
MAX_CONSECUTIVE_FAILURES = 5


class Engine:
    def __init__(self, broker: Broker | None = None) -> None:
        self.broker: Broker = broker or PaperBroker()
        self.running = True
        self.last_scan_at: dict[str, float] = {}
        self.last_manage_at = 0.0
        self.last_regime: str | None = None
        self.last_news_at: float = 0.0
        self.last_keepalive_at = 0.0
        self.consecutive_failures = 0
        self.cycles = 0
        self._news_lock = threading.Lock()
        self._news_thread: threading.Thread | None = None
        self._last_news_bias: dict[str, float] = {}

    # ------------------------------------------------------------ lifecycle
    def install_signal_handlers(self) -> None:
        def _stop(signum, _frame):
            log.info("received signal %s; finishing the current tick", signum)
            self.running = False
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _stop)
            except ValueError:
                pass  # not on the main thread

    def state(self) -> SessionState:
        return clock.resolve(
            equity_enabled=settings.equity_enabled,
            crypto_enabled=settings.crypto_enabled,
            force=repo.kv.get("force_regime", settings.force_regime),
        )

    # ----------------------------------------------------------------- loop
    def run(self) -> None:
        self.install_signal_handlers()
        state = self.state()
        discord.info(
            f"Desk online. {state.label}. "
            f"Next handoff {state.next_handoff_et:%H:%M} ET.\n"
            f"{settings.dashboard_url}",
            channel="engine",
        )

        while self.running:
            try:
                self.tick()
                self.consecutive_failures = 0
            except KeyboardInterrupt:
                self.running = False
            except Exception as exc:
                self._handle_cycle_failure(exc)

            time.sleep(settings.tick_s)

        discord.info("Desk offline. Shutdown was clean.", channel="engine")

    def tick(self) -> None:
        self.cycles += 1
        state = self.state()

        self._announce_handoff(state)
        self._process_commands()
        self._maybe_refresh_news(state)
        self._maybe_keepalive()

        # 1. Positions always come first.
        #
        # Rate-limited to MANAGE_INTERVAL_S rather than run on every tick.
        # Re-marking an option means re-fetching its chain, and doing that
        # every five seconds per position gets the data feed to block us
        # inside a minute. The tick stays fast so commands stay responsive.
        summary: dict = {}
        if self._manage_due():
            self.last_manage_at = time.monotonic()
            summary = position_manager.manage_all(state, self.broker)

        # 2. Scan if this shift is due.
        if self._scan_due(state):
            self._scan(state)

        repo.heartbeats.beat(
            COMPONENT, os.getpid(),
            json.dumps({
                "regime": state.regime.value,
                "cycles": self.cycles,
                "open": repo.positions.open_count(),
                **summary,
            }),
        )

    def _maybe_refresh_news(self, state: SessionState, *, force: bool = False) -> None:
        """
        Interval-driven rolling news on a daemon thread (single-flight).

        The tick that honours stops is never delayed by a model call: this
        method returns immediately after kicking the thread (or no-ops if a
        refresh is already running).
        """
        if not settings.news_enabled:
            return
        # Refresh during live desks and PREP; skip IDLE.
        if state.regime is Regime.IDLE and not force:
            return

        now = time.monotonic()
        if not force and (now - self.last_news_at) < settings.news_refresh_interval_s:
            return

        scope = "CRYPTO" if state.regime is Regime.CRYPTO else "MACRO"
        session_date = state.session_date.isoformat()

        if not self._news_lock.acquire(blocking=False):
            return  # single-flight: a hung refresh must not stack threads
        # Mark the attempt so we don't re-kick every tick while it runs.
        self.last_news_at = now

        def _worker() -> None:
            try:
                from app.agents import news as news_agent
                bias = news_agent.refresh(
                    scope, session_date=session_date, force=force
                )
                self._last_news_bias[scope] = bias
                discord.info(
                    f"News bias [{scope}] {session_date}: **{bias:+.2f}**",
                    channel="news",
                )
                try:
                    repo.sentiment.prune(keep=500)
                except Exception:
                    pass
            except Exception as exc:
                log.warning("news refresh worker failed closed: %s", exc)
            finally:
                self._news_lock.release()

        self._news_thread = threading.Thread(
            target=_worker, name=f"news-{scope}", daemon=True
        )
        self._news_thread.start()

    def news_status(self) -> dict:
        from app.agents import news as news_agent
        st = news_agent.status()
        st["refresh_interval_s"] = settings.news_refresh_interval_s
        st["ttl_hours"] = settings.news_bias_ttl_hours
        st["in_flight"] = self._news_lock.locked()
        st["last_kicked_bias"] = dict(self._last_news_bias)
        return st

    def _maybe_keepalive(self) -> None:
        """
        Opt-in outbound self-ping. Binds no port — the engine process just
        GETs the dashboard healthz so free-tier hosts do not spin down.
        Prefer an external pinger (cron-job.org) when possible; see DEPLOY.md.
        """
        if not settings.keepalive_enabled:
            return
        interval = max(60, settings.keepalive_interval_s)
        if (time.monotonic() - self.last_keepalive_at) < interval:
            return
        self.last_keepalive_at = time.monotonic()
        url = settings.dashboard_url.rstrip("/") + "/healthz"
        try:
            import requests
            requests.get(url, timeout=8)
        except Exception as exc:
            log.debug("keepalive ping failed: %s", exc)

    # ------------------------------------------------------------ cadence
    def _manage_due(self) -> bool:
        """Always true on the first tick, so stops are live immediately."""
        if self.last_manage_at == 0.0:
            return True
        return (time.monotonic() - self.last_manage_at) >= settings.manage_interval_s

    def _scan_due(self, state: SessionState) -> bool:
        if state.regime in (Regime.IDLE, Regime.PREP):
            return False
        if repo.kv.get_bool("halted", False):
            return False

        interval = (settings.scan_interval_equity_s
                    if state.regime is Regime.EQUITY
                    else settings.scan_interval_crypto_s)
        last = self.last_scan_at.get(state.regime.value, 0.0)
        return (time.monotonic() - last) >= interval

    def _scan(self, state: SessionState) -> None:
        adapter = adapter_for_regime(state.regime.value)
        if adapter is None:
            return
        self.last_scan_at[state.regime.value] = time.monotonic()
        outcome = scanner.run_scan(adapter, state, self.broker)

        if outcome.failed and not outcome.ok:
            discord.warn(
                f"Scan {outcome.scan_id} could not price a single symbol. "
                f"The data feed is probably down.",
                channel="scanner",
                dedupe_key="scan-total-failure",
                cooldown_s=1800,
            )

    # ------------------------------------------------------------- handoff
    def _announce_handoff(self, state: SessionState) -> None:
        if self.last_regime == state.regime.value:
            return
        previous, self.last_regime = self.last_regime, state.regime.value
        if previous is None:
            return

        summary = risk.portfolio_summary()
        discord.info(
            f"**Shift change: {previous} \u2192 {state.regime.value}**\n"
            f"{state.label}\n"
            f"Equity {summary['equity']:,.2f} \u00b7 "
            f"{summary['open_count']} open \u00b7 "
            f"realized today {summary['realized_today']:+,.2f}",
            channel="engine",
        )
        repo.events.add("INFO", "engine",
                        f"Shift change {previous} to {state.regime.value}")

    # ------------------------------------------------------------ commands
    def _process_commands(self) -> None:
        while True:
            cmd = repo.commands.claim_next()
            if cmd is None:
                return
            try:
                result = self._run_command(cmd)
                repo.commands.complete(cmd["id"], True, result)
                log.info("command %s (%s) -> %s", cmd["id"], cmd["kind"], result)
            except Exception as exc:
                repo.commands.complete(cmd["id"], False, str(exc))
                log.exception("command %s (%s) failed", cmd["id"], cmd["kind"])

    def _run_command(self, cmd: dict) -> str:
        kind = cmd["kind"]
        payload = json.loads(cmd.get("payload_json") or "{}")

        if kind == "CLOSE_POSITION":
            # Force a manage pass next tick: the book just changed.
            self.last_manage_at = 0.0
            return position_manager.close_manually(
                payload["position_id"], self.broker, payload.get("note", "")
            )

        if kind == "FLATTEN_ALL":
            return position_manager.flatten_all(self.broker)

        if kind == "HALT":
            reason = payload.get("reason", "halted from the dashboard")
            repo.kv.set("halted", "true")
            repo.kv.set("halt_reason", reason)
            discord.warn(f"Trading halted: {reason}", channel="engine")
            return f"Halted: {reason}"

        if kind == "RESUME":
            repo.kv.set("halted", "false")
            repo.kv.set("halt_reason", "")
            discord.info("Trading resumed.", channel="engine")
            return "Resumed."

        if kind == "SCAN_NOW":
            state = self.state()
            if state.regime in (Regime.IDLE, Regime.PREP):
                return f"Nothing to scan during {state.regime.value}."
            self.last_scan_at.pop(state.regime.value, None)
            self._scan(state)
            return f"Scanned the {state.regime.value} universe."

        if kind == "SET_REGIME":
            regime = (payload.get("regime") or "").upper()
            if regime in {"EQUITY", "CRYPTO", "IDLE"}:
                repo.kv.set("force_regime", regime)
                return f"Forced regime {regime}."
            repo.kv.set("force_regime", "")
            return "Regime override cleared."

        if kind == "SET_WEIGHTS":
            from app.engine.scoring import save_weights
            save_weights({k: float(v) for k, v in payload.items()})
            return f"Weights set to {payload}."

        if kind == "REFRESH_NEWS":
            state = self.state()
            scope = (payload.get("scope") or "").upper()
            if scope not in {"MACRO", "CRYPTO"}:
                scope = "CRYPTO" if state.regime is Regime.CRYPTO else "MACRO"
            # Force-kick; single-flight still applies.
            self.last_news_at = 0.0
            self._maybe_refresh_news(state, force=True)
            return f"News refresh kicked for {scope}."

        raise ValueError(f"Unknown command: {kind}")

    # -------------------------------------------------------------- errors
    def _handle_cycle_failure(self, exc: Exception) -> None:
        self.consecutive_failures += 1
        log.exception("engine cycle failed (%d in a row)", self.consecutive_failures)

        level = "CRITICAL" if self.consecutive_failures >= 3 else "WARN"
        discord.send(
            f"Engine cycle failed ({self.consecutive_failures} in a row)\n"
            f"```{type(exc).__name__}: {str(exc)[:600]}```",
            level,  # type: ignore[arg-type]
            channel="engine",
            dedupe_key=f"cycle-fail:{type(exc).__name__}",
            cooldown_s=300,
        )

        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            repo.kv.set("halted", "true")
            repo.kv.set(
                "halt_reason",
                f"auto-halted after {self.consecutive_failures} failed cycles",
            )
            discord.critical(
                f"Auto-halted after {self.consecutive_failures} consecutive "
                f"failed cycles. Open positions are still being managed. "
                f"Resume from the dashboard once the cause is understood.",
                channel="engine",
            )

        time.sleep(BACKOFF_S)
