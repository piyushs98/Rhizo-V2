"""
The web process.

It reads. It does not trade.

The only way this process affects the book is by writing a row to the
`commands` table, which the engine picks up on its next tick. That is
deliberate: post-mortem #11 in v1 was a dashboard that took the trading
process down with it. Here the two are separate OS processes sharing a
WAL-mode SQLite file. Kill the dashboard and the desk keeps trading; kill
the engine and the dashboard tells you so in red.

FastAPI on uvicorn, so a slow client cannot starve a worker thread the way
long-lived connections did under gthread.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import clock
from app.config import assert_valid, settings
from app.db import repositories as repo
from app.db.connection import init_db
from app.engine import risk
from app.engine.scoring import DEFAULT_WEIGHTS, load_weights
from app.logging_setup import setup
from app.resilience.circuit_breaker import all_states

log = setup("web")
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Janus Desk", docs_url="/api/docs", redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    assert_valid()
    init_db()
    log.info("dashboard ready on port %s", settings.port)


# ===========================================================================
# Health
# ===========================================================================
ENGINE_STALE_S = 120


@app.get("/healthz")
def healthz() -> JSONResponse:
    """
    Liveness only. Always 200 if the web process is up.

    For external pingers (cron-job.org, Render's own check) that only need
    to know the process is alive — not whether the engine is trading.
    Use `/health` for real monitoring.
    """
    return JSONResponse({"status": "ok"}, status_code=200)


@app.get("/health")
def health() -> JSONResponse:
    """
    A real health check.

    v1's `/health` returned OK unconditionally, which meant it happily
    reported healthy with a dead bot behind it. This one fails when the
    engine stops beating, so an external monitor can actually page you.
    """
    age = repo.heartbeats.age_seconds("engine")
    engine_ok = age is not None and age < ENGINE_STALE_S
    body = {
        "status": "ok" if engine_ok else "degraded",
        "engine_heartbeat_age_s": round(age, 1) if age is not None else None,
        "engine": "alive" if engine_ok else "not beating",
        "db": "ok",
    }
    return JSONResponse(body, status_code=200 if engine_ok else 503)


# ===========================================================================
# Read endpoints
# ===========================================================================
@app.get("/api/session")
def session():
    state = clock.resolve(
        equity_enabled=settings.equity_enabled,
        crypto_enabled=settings.crypto_enabled,
        force=repo.kv.get("force_regime", settings.force_regime),
    )
    return {
        **state.to_dict(),
        "ribbon": clock.ribbon_segments(state.session_date),
        "day_fraction": (
            state.now_et.hour * 3600 + state.now_et.minute * 60
            + state.now_et.second
        ) / 86400.0,
        "universe": (settings.equity_universe if state.regime.value == "EQUITY"
                     else settings.crypto_universe),
    }


@app.get("/api/portfolio")
def portfolio():
    return risk.portfolio_summary()


@app.get("/api/positions")
def positions():
    return {"positions": [p.to_dict() for p in repo.positions.open_positions()]}


@app.get("/api/positions/{position_id}")
def position_detail(position_id: str):
    pos = repo.positions.get(position_id)
    if pos is None:
        raise HTTPException(404, "No such position.")
    return {"position": pos.to_dict(), "events": repo.positions.events(position_id)}


@app.get("/api/history")
def history(limit: int = 100):
    return {"positions": [p.to_dict() for p in repo.positions.closed(limit)]}


@app.get("/api/scan/latest")
def latest_scan():
    return repo.scans.latest() or {"scan_id": None, "results": []}


@app.get("/api/scans")
def scans(limit: int = 20):
    return {"scans": repo.scans.recent(limit)}


@app.get("/api/equity-curve")
def equity_curve(limit: int = 500):
    return {"points": repo.ledger.curve(limit)}


@app.get("/api/events")
def events(limit: int = 60, level: str | None = None):
    return {"events": repo.events.recent(limit, level)}


@app.get("/api/system")
def system():
    return {
        "heartbeats": repo.heartbeats.all(),
        "breakers": all_states(),
        "weights": load_weights(),
        "default_weights": DEFAULT_WEIGHTS,
        "config": settings.redacted(),
        "commands": repo.commands.recent(10),
    }


@app.get("/api/sentiment")
def sentiment_api():
    """Latest PREP-shift news bias, if any and still within TTL."""
    from app.config import settings as cfg
    row = repo.sentiment.latest(max_age_hours=cfg.news_bias_ttl_hours)
    if row is None:
        return {
            "bias": 0.0,
            "fresh": False,
            "session_date": None,
            "created_at": None,
            "note": "",
            "source": "",
        }
    return {
        "bias": float(row["bias"]),
        "fresh": True,
        "session_date": row.get("session_date"),
        "created_at": row.get("created_at"),
        "note": row.get("note") or "",
        "source": row.get("source") or "",
    }


@app.get("/api/overview")
def overview():
    """One call for the whole dashboard. Fewer round trips, one clock."""
    return {
        "session": session(),
        "portfolio": portfolio(),
        "positions": positions()["positions"],
        "scan": latest_scan(),
        "events": repo.events.recent(30),
        "equity_curve": repo.ledger.curve(200),
        "sentiment": sentiment_api(),
        "system": {
            "heartbeats": repo.heartbeats.all(),
            "breakers": all_states(),
        },
    }


# ===========================================================================
# Commands (queued, never executed here)
# ===========================================================================
class ClosePayload(BaseModel):
    position_id: str
    note: str = ""


class HaltPayload(BaseModel):
    reason: str = "halted from the dashboard"


class RegimePayload(BaseModel):
    regime: str = Field("", description="EQUITY, CRYPTO, IDLE, or empty to clear")


def _queued(kind: str, payload: dict | None = None):
    cmd_id = repo.commands.enqueue(kind, payload or {})
    return {"queued": True, "command_id": cmd_id, "kind": kind}


@app.post("/api/commands/close")
def cmd_close(body: ClosePayload):
    if repo.positions.get(body.position_id) is None:
        raise HTTPException(404, "No such position.")
    return _queued("CLOSE_POSITION", body.model_dump())


@app.post("/api/commands/flatten")
def cmd_flatten():
    return _queued("FLATTEN_ALL")


@app.post("/api/commands/halt")
def cmd_halt(body: HaltPayload):
    return _queued("HALT", body.model_dump())


@app.post("/api/commands/resume")
def cmd_resume():
    return _queued("RESUME")


@app.post("/api/commands/scan")
def cmd_scan():
    return _queued("SCAN_NOW")


@app.post("/api/commands/regime")
def cmd_regime(body: RegimePayload):
    return _queued("SET_REGIME", body.model_dump())


# ===========================================================================
# Static dashboard
# ===========================================================================
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
