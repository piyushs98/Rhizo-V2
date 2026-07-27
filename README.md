# Janus Desk

A paper-trading desk that runs two shifts.

```
09:00–09:30 ET   PREP     warm caches, build the morning read
09:30–16:00 ET   EQUITY   options on the equity universe
16:00–09:00 ET   CRYPTO   spot crypto through the night
weekends, holidays        CRYPTO all day
```

Named for the two-faced god: one face watching the equity session, the other
watching the tape after the bell. The shift you are in is a pure function of
the clock, and everything downstream — which universe to scan, which
instruments to trade, which exit rules apply — follows from it.

---

## Start here

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

python scripts/doctor.py           # preflight: config, database, data feeds
PYTHONPATH=. python -m pytest      # 162 tests, ~1 second, no network
python scripts/simulate.py         # full pipeline against a synthetic market
python run.py                      # engine + dashboard
```

Then open <http://localhost:8000>.

`run.py` is a supervisor. It starts two child processes and restarts either
one if it dies:

| Process | Entry point | Does | Never does |
|---|---|---|---|
| Engine | `run_engine.py` | scans, sizes, fills, manages exits | bind a port |
| Dashboard | `run_web.py` | serves the UI and a read-only API | place a trade |

They share one WAL-mode SQLite file and no memory. Kill the dashboard and the
desk keeps trading. Kill the engine and the dashboard says so, in red.

---

## What is in here

```
run.py                  supervisor: spawns and restarts both processes
run_engine.py           trading engine (no socket, no port)
run_web.py              dashboard (uvicorn)

app/
  config.py             every setting, validated at boot; refuses to start if broken
  clock.py              the session router — the spine of the system
  calendar_nyse.py      NYSE holidays, computed, no expiry date

  db/
    schema.sql          one source of truth; UNIQUE key on the trade signal
    connection.py       WAL mode, thread-local connections
    repositories.py     all SQL; idempotent writes; enforced state transitions

  domain/models.py      Position, ExitPlan, OrderIntent, ScoreCard, enums

  data/
    http.py             shared session, browser UA, timeout on every request
    providers.py        Yahoo (equities) · Coinbase → Kraken (crypto)

  markets/adapters.py   EquityOptionsAdapter · CryptoSpotAdapter

  engine/
    indicators.py       ATR, RSI, EMA, pivots — pure functions
    scoring.py          three deterministic pillars; the only EXECUTE authority
    risk.py             ten pre-trade gates, each with a named refusal
    scanner.py          market-agnostic scan pipeline
    exit_rules.py       stop / trail / target / time / flatten, in that order
    position_manager.py re-marks and closes; runs before scanning, every tick
    scheduler.py        the immortal loop

  broker/paper.py       simulated fills that cross the spread and pay fees
  llm/chain.py          Gemini → DeepSeek → silence. Commentary only.
  notify/discord.py     chunked, rate-limited, never raises
  resilience/           timeouts · circuit breakers · OS-level instance lock
  web/                  FastAPI + a vanilla-JS dashboard, no build step

scripts/
  doctor.py             preflight check
  simulate.py           offline end-to-end run, no network

tests/                  162 tests: scoring, risk, exits, news, scalp, notify, deploy
```

---

## The three problems this was built to solve

### 1. Every scan opened another position in the same name

The old system scored a ticker, saw EXECUTE, and bought — with no check for
whether it already held that position. Ten scans meant ten NVDA contracts.

Fixed in two places, structurally:

- `positions.idempotency_key` is a **UNIQUE column**. Its value is
  `MARKET:UNDERLYING:DIRECTION:SESSION`. A repeat signal in the same shift
  hits the constraint and is discarded by the database, not by an `if`.
- `PositionRepo.open_position()` returns `(position, created=False)` on a
  repeat instead of raising. Re-scanning a name you hold is the normal case,
  not an error.
- `risk.check()` reports it cleanly as `DUPLICATE` before it gets that far,
  so the dashboard can show you why.

The first test in `tests/test_positions.py` runs ten scans and asserts one
position. If someone breaks this, the suite fails in one second.

### 2. Nothing ever closed a position

The old tracker was fully written and commented out of the boot path. Trades
opened and stayed open forever; realized PnL could never be booked.

Here, `position_manager.manage_all()` runs on **every engine tick, before
scanning**. Entry and exit are the same loop — there is no separate agent to
switch off. Each position carries an `ExitPlan` fixed at entry, and
`exit_rules.evaluate()` is a pure function of `(position, mark, clock)` with
explicit precedence:

```
stop loss  →  VWAP break (scalp)  →  trailing stop  →  take profit  →  time stop  →  session flatten
```

Deterministic, cheap, and covered by unit tests. A language model can comment
on a position; it cannot close one. The PREP news agent is the sole exception
where a model influences a score — and it only emits a float in [-1, 1].

### 3. The dashboard was a liability

The old one starved the web workers with a long-lived SSE stream and took the
service down with it. This one is a separate process, polls a single JSON
endpoint every five seconds, and writes to a `commands` table rather than
touching trading state. The engine picks commands up on its next tick.

Two elements do real work:

- **The session ribbon** across the top is the 24-hour day with both shifts
  drawn as territories and a live playhead. The page's accent colour follows
  the active shift — brass while equities trade, moonlight once crypto takes
  the book. You can tell what the desk is doing from across the room.
- **The distance-to-exit bar** on each position puts stop on the left, target
  on the right, and the current mark as a marker between them. That is the
  "hold or close" question answered without arithmetic.

The scan board below shows every symbol with its three pillar scores and,
when it did not fire, **the name of the gate that blocked it**. "Why didn't
it trade" is a glance, not an investigation.

---

## Operating it

**Kill switch.** *Halt trading* on the dashboard stops new positions
immediately. Open positions keep being managed — halting entries must never
mean abandoning exits.

**Alerts.** Set `DISCORD_WEBHOOK` for fills, closes and warnings. Set
`DISCORD_CRITICAL_WEBHOOK` separately if you want the wake-me-up channel to
be different from the noise channel.

**Health.** `/healthz` is liveness (always 200). `/health` returns 503 when
the engine heartbeat is more than 120 seconds stale — use that for real
monitoring. (The old one returned OK unconditionally.)

**Tuning.** Everything lives in `.env`. The scoring weights are also
adjustable at runtime and stored in the database; they must sum to 100.

---

## Deploying

See **[DEPLOY.md](DEPLOY.md)** for free vs paid, keep-alive (cron-job.org),
and the storage guard.

- `render.yaml` — free tier. Requires `EPHEMERAL_STORAGE_ACK=true` because
  there is no disk; the book is wiped on restart.
- `render.paid.yaml` — starter plan with a 1 GB disk at `/var/data`. Prefer
  this for anything beyond a demo.

If you later want the engine and the dashboard as separate services, nothing
in the application changes — point one service at `run_engine.py`, the other
at `run_web.py`, and give them a shared Postgres instead of the file. The
repository layer is the only thing that would need a new backend.

---

## What this is and is not

It is a paper-trading system. Fills are simulated, they cross the spread, and
they pay modelled fees — so the PnL is roughly comparable to a live account
rather than flatteringly better than one.

The scoring model is a reasonable, legible starting point. It is not a proven
edge, and nothing here has been backtested. Before this is worth connecting
to real money, the honest next step is to run the scoring and exit logic over
historical data and find out whether the strategy makes money at all. The
architecture is built to make that easy — `scoring.py` and `exit_rules.py` are
pure functions with no I/O, so a backtester only has to feed them bars.

See `ARCHITECTURE.md` for the design decisions and where to extend.
