# Architecture

Why this is shaped the way it is, and where to extend it.

Written for whoever picks this up next — human or model — working in a
terminal without the context of the conversation that produced it.

---

## 1. The organizing principle

One clock decides everything.

```
app/clock.py :: resolve() → SessionState(regime, session_date, next_handoff)
```

`Regime` is `PREP | EQUITY | CRYPTO | IDLE`. Nothing else in the codebase
computes market hours. The scheduler asks the clock what shift it is, hands
the matching adapter to the scanner, and the scanner does not know whether it
is looking at options or spot.

This is what makes the two-shift design cheap rather than a special case
threaded through every function. Adding a third desk — futures, FX — is a new
adapter and one line in `adapter_for_regime()`.

**Session keys.** `session_key(state)` returns `EQ-2026-07-24` or
`CX-2026-07-24`. The crypto key deliberately anchors to the date the
overnight block *started*, so 23:00 Monday and 02:00 Tuesday are the same
shift. That string is part of the trade idempotency key, which is why a
signal cannot fire twice across midnight.

---

## 2. Process topology

```
run.py  (supervisor)
  ├── run_engine.py   trading loop      · holds an flock · opens no socket
  └── run_web.py      uvicorn dashboard · reads only     · binds $PORT

                    both → data/janus.db (WAL)
```

Three failures in the previous system came from putting these in one process:
a port collision when the trading module called `keep_alive()`, a double-spawn
when two threads raced through the boot path, and a dashboard stream that
starved the web workers and took the health check down with it.

Separating them removes all three by construction, and it removes the
`--workers 1` constraint that made the whole service fragile.

**Why one Render service, not two.** Render disks attach to exactly one
service, and SQLite needs a shared filesystem. The supervisor gives process
isolation at single-service cost. When you outgrow it, move to Postgres and
split the services — no application code changes.

**Why the flock and not a mutex.** An in-process lock does nothing against a
second OS process. `resilience/singleton.py` takes an advisory file lock, so
two engines cannot run against the same book regardless of what launched them.

---

## 3. State

One source of truth: SQLite, WAL mode. **There is no JSON position file.**

The previous system dual-wrote to JSON and SQLite and reconciled them with a
sync routine. That produced two distinct failures — positions lost on restart
when an empty seed overwrote the file, and ghost trades from a selective
DELETE/UPSERT. Both were designed away rather than patched.

**Tables that matter:**

| Table | Purpose |
|---|---|
| `positions` | current state. `idempotency_key` is UNIQUE |
| `position_events` | append-only audit; never updated, never deleted |
| `scan_results` | every symbol, every scan, including `blocked_by` |
| `commands` | dashboard → engine. The only write path from the web process |
| `ledger` | cash, realized PnL, fees, counts |
| `equity_curve` | snapshot per manage tick |

**Transitions are enforced.** `LEGAL_TRANSITIONS` in `domain/models.py`
defines the state machine; `PositionRepo._transition` rejects anything not in
it. A CLOSED position cannot reopen. Closing twice is a no-op, not a
double-booking.

---

## 4. The LLM boundary

`app/llm/chain.py` produces **commentary**. It is written to
`scan_results.commentary`, rendered on the dashboard, and read by a human.

It is never parsed for scoring. The sole exception is the PREP news agent
(`app/agents/news.py`), which asks the model for strict JSON and emits a
single float in [-1, 1]. That float is stored as a REAL and passed into
`score_sentiment(news_bias: float)`. Scoring imports neither the agent nor
the LLM chain. Prose, malformed JSON, out-of-range values, and provider
outages all resolve to 0.0.

The reason the boundary is this strict is specific. The previous system did
a substring search for the word "warning" inside a manager's prose to decide
whether to apply a scoring bonus. Some phrasings contained the word
incidentally, the bonus was suppressed, every score capped around 50–65, and
executions stopped silently for an unknown period.

Two tests still enforce the structural fix:

- `test_no_scoring_function_accepts_text` — no scoring function has a `str`
  parameter that could carry a narrative.
- `test_scoring_module_does_not_import_the_llm` — checked against the parsed
  import graph, so an aliased import cannot hide.

If both providers are down, the desk trades exactly as it otherwise would,
with an empty comment field and a neutral news bias.

---

## 5. Resilience

| Concern | Mechanism | File |
|---|---|---|
| Hung sockets | wall-clock `budget()` around every external call | `resilience/timeouts.py` |
| Provider outage | circuit breaker, fail-closed, per dependency | `resilience/circuit_breaker.py` |
| Double-spawn | OS advisory file lock | `resilience/singleton.py` |
| Rate limits | browser UA, connection reuse, deliberate pacing | `data/http.py` |
| Venue failure | provider chain: Coinbase → Kraken | `data/providers.py` |
| Model outage | provider chain: Gemini → DeepSeek → silence | `llm/chain.py` |
| Cycle failure | log, alert, back off, continue; auto-halt after 5 | `engine/scheduler.py` |
| One bad symbol | per-symbol try in the scan loop | `engine/scanner.py` |

**Budget arithmetic.** `Settings.validate()` refuses to boot if the worst-case
time for one symbol exceeds the scan interval. Timeouts that do not add up are
a scheduled outage.

**Auto-halt.** Five consecutive failed cycles halts new entries and pages you.
Open positions keep being managed — halting entries must never mean
abandoning exits.

---

## 6. Where to extend

### Add a symbol
`.env` → `EQUITY_UNIVERSE` or `CRYPTO_UNIVERSE`. Nothing else.

### Change how things are scored
`app/engine/scoring.py`. Three pillars, each a pure function returning
`(score, inputs_dict)`. Everything in `inputs` shows up in `scan_results`
and on the dashboard, so add diagnostics freely.

Keep the contract: **floats in, floats out.** The tests will fail if you add
a `str` parameter, and that is deliberate.

### Change when things close
`app/engine/exit_rules.py`. One function, `evaluate()`, with documented
precedence (stop → VWAP break → trail → target → time → flatten). Add a rule
by inserting a block in priority order and a case in `ExitReason`. BTC scalp
plans are built in `app/engine/scalping.py` and attached on the assessment;
the scanner uses an adapter-supplied plan when present.

### Add a market
1. Implement the `MarketAdapter` protocol in `app/markets/adapters.py`:
   `universe()`, `build_context()`, `assess()`, `mark()`, plus the
   `market` / `multiplier` / `whole_units` attributes.
2. Add the `Market` enum value.
3. Wire it in `adapter_for_regime()`.
4. Give it exit percentages in `Settings.exit_params()`.

Step 4 matters. Option premiums swing 40% in a session; BTC spot does not.
Sharing one set of percentages across markets means one of them can only ever
exit on the time stop. The offline simulation caught exactly that.

### Swap in real market data
`app/data/providers.py`. Implement `quote()` and `bars()` (and
`option_chain()` for equities) against Polygon, Tradier, or Alpaca. Keep the
`Quote` / `Bar` / `OptionQuote` dataclasses and nothing above the data layer
changes.

yfinance is a scrape, not an API. It is fenced behind a breaker for that
reason. It is fine for paper; it is not fine for money.

### Connect a real broker
`app/broker/base.py` defines the protocol; `paper.py` implements it. A live
broker implements the same two methods. `OrderIntent` already carries what a
real fill needs.

Before you do this, read the last section of the README.

### Add a backtester
This is the highest-value next piece and the architecture is already shaped
for it. `scoring.py`, `exit_rules.py` and `indicators.py` are pure functions
with no I/O. A backtester needs to:

1. Load historical bars.
2. Walk them forward, calling `scoring.compose()` at each step.
3. Call `exit_rules.evaluate()` on open positions.
4. Book fills through `PaperBroker` so slippage and fees are included.

`scripts/simulate.py` is 80% of that harness already — it runs the real
scanner, risk gates, broker and position manager against a synthetic series.
Point it at real historical data and you have a backtest.

---

## 7. Things deliberately not built

Listed so nobody assumes they were forgotten.

- **Multi-leg options.** Long single-leg only. Spreads need a different
  position model (multiple instruments per position) and a different risk
  calculation. The `Position` schema would need a `legs` table.
- **Shorting.** `Direction` has no `SHORT_*` values. Adding them means
  reversing the sign in `compute_unrealized`, `evaluate()`, and the sizing
  math. Do it deliberately, with tests.
- **Streaming prices.** Everything is polled. Fine at 15-minute cadence;
  wrong for anything intraday-fast.
- **Order types.** The paper broker fills at the mark plus slippage. No
  limits, no stops resting at the venue. A real broker integration would
  change that.
- **Authentication.** The dashboard is unauthenticated. Do not expose it on a
  public URL without putting something in front of it.
- **Backtesting.** See above. It is the thing most worth building next.

---

## 8. Post-mortem trace

Each documented failure of the previous system, and what replaced it.

| # | v1 failure | Resolution |
|---|---|---|
| 1 | Trading module bound `$PORT` | Engine opens no socket |
| 2 | Yahoo rate-limited on datacentre IP | Browser UA, session reuse, pacing |
| 3 | Missing env var found at runtime | `assert_valid()` at every entrypoint |
| 4 | Substring match on LLM prose capped scores | Scoring takes no strings; two tests enforce it |
| 5 | Discord 400 on >2000 chars | Chunked at 1900 |
| 6 | Holiday set expiring 2027-12-24 | Computed calendar, no expiry |
| 7 | Regex parser broke on free-form tags | Nothing parses model output |
| 8 | Positions lost on restart | One store, no JSON, no reconciliation |
| 9 | Ghost trades from selective DELETE | Append-only events, enforced transitions |
| 10 | No timeout on data calls | `budget()` on every external call |
| 11 | SSE starved web workers | Separate process, polling, uvicorn |
| 12 | Boot race double-spawned the bot | OS-level flock |
| 13 | Ambiguous "market closed" state | Explicit `Regime` enum |
| 14 | Boot overlay never dismissed | Dashboard renders from one endpoint; no boot gate |
| 15 | Procfile drift broke the deploy | One `Procfile`, one `render.yaml`, `doctor.py` |
| 16 | Silent scoring degradation | Every pillar input persisted per symbol per scan |
| 17 | Single LLM provider outage | Failover chain, and nothing depends on it |
| 18 | Unbounded LLM calls hung the worker | Per-call and per-chain budgets |
| — | **Position tracker commented out of boot** | Exit management runs every tick, before scanning |


---

## 9. Market data budget

`app/resilience/governor.py` enforces a rolling 60s request ceiling. Alpaca
calls acquire a slot before each HTTP request; a venue 429 fills the window
immediately. Equity `build_context()` batches quotes and bars; option chains
are deferred and gated by an exact bound so a candidate that cannot clear the
threshold even with perfect liquidity never spends a request.
