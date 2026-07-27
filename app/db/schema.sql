-- ===========================================================================
-- Janus Desk schema.
--
-- ONE source of truth. Post-mortems #8 and #9 in v1 came from a JSON file and
-- a SQLite table both claiming to hold open positions, reconciled by a sync
-- routine. There is no JSON file here. If the dashboard wants a snapshot, it
-- reads from these tables.
--
-- The `idempotency_key` UNIQUE constraint is the structural fix for the bug
-- where every scan opened a fresh position in the same symbol. The database
-- refuses the duplicate; the application does not have to remember to check.
-- ===========================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------- positions
CREATE TABLE IF NOT EXISTS positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id         TEXT    NOT NULL UNIQUE,
    idempotency_key     TEXT    NOT NULL UNIQUE,   -- <<< the duplicate fix

    market              TEXT    NOT NULL,          -- EQUITY_OPTION | CRYPTO_SPOT
    underlying          TEXT    NOT NULL,          -- NVDA | BTC-USD
    instrument          TEXT    NOT NULL,          -- OCC symbol, or BTC-USD
    direction           TEXT    NOT NULL,          -- LONG_CALL | LONG_PUT | LONG_SPOT
    status              TEXT    NOT NULL,          -- PENDING|OPEN|CLOSING|CLOSED|REJECTED

    quantity            REAL    NOT NULL,
    multiplier          REAL    NOT NULL DEFAULT 1,

    entry_price         REAL,
    entry_ts            TEXT,
    entry_notional      REAL,

    exit_price          REAL,
    exit_ts             TEXT,
    exit_reason         TEXT,

    -- exit plan, written once at entry, evaluated deterministically
    stop_price          REAL,
    target_price        REAL,
    trail_activate_at   REAL,
    trail_giveback_pct  REAL,
    trail_high_water    REAL,
    time_stop_ts        TEXT,

    -- BTC multi-layer scalp fields (null/0 for equity options)
    scalp               INTEGER NOT NULL DEFAULT 0,
    vwap_floor          REAL,
    r_unit              REAL,

    -- live mark, refreshed by the position manager
    mark_price          REAL,
    mark_ts             TEXT,
    unrealized_pnl      REAL,
    realized_pnl        REAL,
    fees                REAL    NOT NULL DEFAULT 0,

    open_scan_id        TEXT,
    entry_score         REAL,
    session_key         TEXT,
    notes               TEXT,
    meta_json           TEXT    NOT NULL DEFAULT '{}',

    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_positions_status      ON positions(status);
CREATE INDEX IF NOT EXISTS ix_positions_underlying  ON positions(underlying, status);
CREATE INDEX IF NOT EXISTS ix_positions_session     ON positions(session_key);
CREATE INDEX IF NOT EXISTS ix_positions_exit_ts     ON positions(exit_ts);

-- Append-only audit of every state transition. Never updated, never deleted.
CREATE TABLE IF NOT EXISTS position_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id  TEXT    NOT NULL,
    ts           TEXT    NOT NULL,
    event        TEXT    NOT NULL,     -- OPENED|MARKED|TRAIL_RAISED|CLOSE_REQUESTED|CLOSED|REJECTED
    from_status  TEXT,
    to_status    TEXT,
    price        REAL,
    detail       TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (position_id) REFERENCES positions(position_id)
);
CREATE INDEX IF NOT EXISTS ix_pos_events ON position_events(position_id, id);

-- ------------------------------------------------------------------- scans
CREATE TABLE IF NOT EXISTS scans (
    scan_id        TEXT PRIMARY KEY,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    regime         TEXT NOT NULL,
    market         TEXT NOT NULL,
    session_key    TEXT,
    symbols_total  INTEGER NOT NULL DEFAULT 0,
    symbols_ok     INTEGER NOT NULL DEFAULT 0,
    symbols_failed INTEGER NOT NULL DEFAULT 0,
    executed       INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER,
    status         TEXT NOT NULL DEFAULT 'RUNNING',   -- RUNNING|OK|PARTIAL|ABORTED
    note           TEXT
);
CREATE INDEX IF NOT EXISTS ix_scans_started ON scans(started_at DESC);

-- Every symbol evaluated in every scan, with the full pillar breakdown and,
-- critically, WHY it did not execute. v1 could not answer that question.
CREATE TABLE IF NOT EXISTS scan_results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id        TEXT NOT NULL,
    ts             TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    market         TEXT NOT NULL,
    total_score    REAL,
    liquidity      REAL,
    technical      REAL,
    sentiment      REAL,
    verdict        TEXT NOT NULL,     -- EXECUTE|PASS|BLOCKED|ERROR
    blocked_by     TEXT,              -- risk gate name, when verdict=BLOCKED
    reason         TEXT,
    instrument     TEXT,
    ref_price      REAL,
    commentary     TEXT,              -- LLM text. Advisory only. Never scored.
    detail_json    TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (scan_id) REFERENCES scans(scan_id)
);
CREATE INDEX IF NOT EXISTS ix_scan_results ON scan_results(scan_id);
CREATE INDEX IF NOT EXISTS ix_scan_results_sym ON scan_results(symbol, ts DESC);

-- ------------------------------------------------------------------ ledger
CREATE TABLE IF NOT EXISTS ledger (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    starting_capital  REAL NOT NULL,
    cash              REAL NOT NULL,
    realized_pnl      REAL NOT NULL DEFAULT 0,
    fees_paid         REAL NOT NULL DEFAULT 0,
    trades_opened     INTEGER NOT NULL DEFAULT 0,
    trades_closed     INTEGER NOT NULL DEFAULT 0,
    wins              INTEGER NOT NULL DEFAULT 0,
    losses            INTEGER NOT NULL DEFAULT 0,
    peak_equity       REAL NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_curve (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    cash         REAL NOT NULL,
    open_value   REAL NOT NULL,
    equity       REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    open_count   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_equity_ts ON equity_curve(ts DESC);

-- --------------------------------------------------- dashboard -> engine
-- The web process never mutates trading state directly. It writes a command
-- row; the engine picks it up on its next tick. Auditable, race-free, and it
-- means the dashboard can crash without touching the book.
CREATE TABLE IF NOT EXISTS commands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL,     -- CLOSE_POSITION|HALT|RESUME|SCAN_NOW|SET_REGIME|FLATTEN_ALL
    payload_json TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'QUEUED',   -- QUEUED|DONE|FAILED
    picked_at    TEXT,
    finished_at  TEXT,
    result       TEXT,
    source       TEXT NOT NULL DEFAULT 'dashboard'
);
CREATE INDEX IF NOT EXISTS ix_commands_status ON commands(status, id);

-- ------------------------------------------------------------- operational
CREATE TABLE IF NOT EXISTS heartbeats (
    component  TEXT PRIMARY KEY,     -- engine | scanner | position_manager | web
    ts         TEXT NOT NULL,
    pid        INTEGER,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL,          -- INFO|WARN|CRITICAL
    channel  TEXT NOT NULL,          -- engine|scanner|risk|broker|data|llm|web
    message  TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(id DESC);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- PREP-shift news bias. One numeric score per run; never prose in the
-- control path. Scoring reads the float via SentimentRepo.latest().
CREATE TABLE IF NOT EXISTS sentiment (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date  TEXT    NOT NULL,
    bias          REAL    NOT NULL,          -- clamped [-1, 1]
    source        TEXT    NOT NULL DEFAULT '',
    raw_json      TEXT    NOT NULL DEFAULT '',
    note          TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sentiment_created ON sentiment(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_sentiment_session ON sentiment(session_date);
