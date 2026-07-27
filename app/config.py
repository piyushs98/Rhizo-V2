"""
Configuration. Loaded once, validated at import, fail-fast.

Design rule (post-mortem #15, #3): the process must refuse to start with a
broken config rather than discovering a missing key at 09:15 on a trading day.
Every value here is overridable by environment variable or .env file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- .env loader
ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader. No dependency; real env always wins."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv(ROOT / ".env")


# ------------------------------------------------------------------- coercion
def _s(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _i(key: str, default: int) -> int:
    try:
        return int(_s(key) or default)
    except ValueError:
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(_s(key) or default)
    except ValueError:
        return default


def _b(key: str, default: bool = False) -> bool:
    v = _s(key).lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


def _list(key: str, default: list[str]) -> list[str]:
    raw = _s(key)
    if not raw:
        return list(default)
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


class ConfigError(RuntimeError):
    """Raised at boot when configuration is unusable."""


# Ten-ticker equity universe. Locked unless ALLOW_CUSTOM_UNIVERSE=true.
CANONICAL_EQUITY_UNIVERSE: list[str] = [
    "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "SPY", "QQQ", "IWM",
]


# ------------------------------------------------------------------ dataclass
@dataclass(frozen=True)
class Settings:
    # --- identity / environment
    env: str = field(default_factory=lambda: _s("ENV", "development"))
    dashboard_url: str = field(
        default_factory=lambda: _s("DASHBOARD_URL")
        or _s("RENDER_EXTERNAL_URL")
        or "http://localhost:8000"
    )

    # --- storage. Single source of truth. One file, WAL mode.
    db_path: str = field(
        default_factory=lambda: _s("DB_PATH", str(ROOT / "data" / "janus.db"))
    )
    log_dir: str = field(default_factory=lambda: _s("LOG_DIR", str(ROOT / "logs")))
    log_level: str = field(default_factory=lambda: _s("LOG_LEVEL", "INFO").upper())
    log_json: bool = field(default_factory=lambda: _b("LOG_JSON", True))
    # Free-tier Render has no disk. Without this ack the process refuses to
    # boot rather than silently wipe the book on every restart.
    ephemeral_storage_ack: bool = field(
        default_factory=lambda: _b("EPHEMERAL_STORAGE_ACK", False)
    )
    allow_custom_universe: bool = field(
        default_factory=lambda: _b("ALLOW_CUSTOM_UNIVERSE", False)
    )

    # --- web
    port: int = field(default_factory=lambda: _i("PORT", 8000))
    host: str = field(default_factory=lambda: _s("HOST", "0.0.0.0"))

    # --- capital
    starting_capital: float = field(
        default_factory=lambda: _f("STARTING_CAPITAL", 100_000.0)
    )

    # --- universes
    equity_universe: list[str] = field(
        default_factory=lambda: _list("EQUITY_UNIVERSE", CANONICAL_EQUITY_UNIVERSE)
    )
    crypto_universe: list[str] = field(
        default_factory=lambda: _list("CRYPTO_UNIVERSE", ["BTC-USD"])
    )

    # --- cadence (seconds)
    scan_interval_equity_s: int = field(
        default_factory=lambda: _i("SCAN_INTERVAL_EQUITY_S", 900)
    )
    scan_interval_crypto_s: int = field(
        default_factory=lambda: _i("SCAN_INTERVAL_CRYPTO_S", 900)
    )
    manage_interval_s: int = field(default_factory=lambda: _i("MANAGE_INTERVAL_S", 60))
    tick_s: int = field(default_factory=lambda: _i("TICK_S", 5))
    inter_symbol_sleep_s: float = field(
        default_factory=lambda: _f("INTER_SYMBOL_SLEEP_S", 2.0)
    )

    # --- risk. Every one of these was absent in v1.
    execute_threshold: float = field(
        default_factory=lambda: _f("EXECUTE_THRESHOLD", 70.0)
    )
    risk_pct_per_trade: float = field(
        default_factory=lambda: _f("RISK_PCT_PER_TRADE", 0.02)
    )
    max_open_positions: int = field(default_factory=lambda: _i("MAX_OPEN_POSITIONS", 5))
    max_positions_per_underlying: int = field(
        default_factory=lambda: _i("MAX_POSITIONS_PER_UNDERLYING", 1)
    )
    max_new_positions_per_day: int = field(
        default_factory=lambda: _i("MAX_NEW_POSITIONS_PER_DAY", 6)
    )
    daily_loss_limit_pct: float = field(
        default_factory=lambda: _f("DAILY_LOSS_LIMIT_PCT", 0.05)
    )
    reentry_cooldown_min: int = field(
        default_factory=lambda: _i("REENTRY_COOLDOWN_MIN", 120)
    )

    # --- exit plan defaults (deterministic; no LLM in this path, ever)
    #
    # These MUST differ by market. An option premium routinely swings 40% in a
    # day; BTC spot does not. Using one set of percentages for both means the
    # crypto book can only ever exit on the time stop, which the offline
    # simulation caught immediately.
    stop_loss_pct_equity: float = field(
        default_factory=lambda: _f("STOP_LOSS_PCT_EQUITY", 0.35)
    )
    take_profit_pct_equity: float = field(
        default_factory=lambda: _f("TAKE_PROFIT_PCT_EQUITY", 0.60)
    )
    trail_activate_pct_equity: float = field(
        default_factory=lambda: _f("TRAIL_ACTIVATE_PCT_EQUITY", 0.30)
    )
    trail_giveback_pct_equity: float = field(
        default_factory=lambda: _f("TRAIL_GIVEBACK_PCT_EQUITY", 0.20)
    )

    stop_loss_pct_crypto: float = field(
        default_factory=lambda: _f("STOP_LOSS_PCT_CRYPTO", 0.030)
    )
    take_profit_pct_crypto: float = field(
        default_factory=lambda: _f("TAKE_PROFIT_PCT_CRYPTO", 0.055)
    )
    trail_activate_pct_crypto: float = field(
        default_factory=lambda: _f("TRAIL_ACTIVATE_PCT_CRYPTO", 0.025)
    )
    trail_giveback_pct_crypto: float = field(
        default_factory=lambda: _f("TRAIL_GIVEBACK_PCT_CRYPTO", 0.35)
    )

    max_hold_hours_equity: float = field(
        default_factory=lambda: _f("MAX_HOLD_HOURS_EQUITY", 48.0)
    )
    max_hold_hours_crypto: float = field(
        default_factory=lambda: _f("MAX_HOLD_HOURS_CRYPTO", 24.0)
    )
    min_trade_notional: float = field(
        default_factory=lambda: _f("MIN_TRADE_NOTIONAL", 25.0)
    )
    flatten_equity_at_close: bool = field(
        default_factory=lambda: _b("FLATTEN_EQUITY_AT_CLOSE", False)
    )

    # --- options contract selection
    target_dte_min: int = field(default_factory=lambda: _i("TARGET_DTE_MIN", 7))
    target_dte_max: int = field(default_factory=lambda: _i("TARGET_DTE_MAX", 45))
    max_spread_pct: float = field(default_factory=lambda: _f("MAX_SPREAD_PCT", 0.12))
    min_open_interest: int = field(default_factory=lambda: _i("MIN_OPEN_INTEREST", 250))

    # --- network budgets. Sum must stay under any process supervisor timeout.
    http_timeout_s: float = field(default_factory=lambda: _f("HTTP_TIMEOUT_S", 15.0))
    data_call_budget_s: float = field(
        default_factory=lambda: _f("DATA_CALL_BUDGET_S", 20.0)
    )
    llm_call_budget_s: float = field(
        default_factory=lambda: _f("LLM_CALL_BUDGET_S", 20.0)
    )
    llm_chain_budget_s: float = field(
        default_factory=lambda: _f("LLM_CHAIN_BUDGET_S", 45.0)
    )

    # --- resilience
    breaker_threshold: int = field(default_factory=lambda: _i("BREAKER_THRESHOLD", 5))
    breaker_cooldown_s: int = field(
        default_factory=lambda: _i("BREAKER_COOLDOWN_S", 900)
    )

    # --- providers (all optional; system degrades, never dies)
    gemini_api_key: str = field(default_factory=lambda: _s("GEMINI_API_KEY"))
    gemini_model: str = field(
        default_factory=lambda: _s("GEMINI_MODEL", "gemini-2.5-flash")
    )
    deepseek_api_key: str = field(default_factory=lambda: _s("DEEPSEEK_API_KEY"))
    deepseek_model: str = field(
        default_factory=lambda: _s("DEEPSEEK_MODEL", "deepseek-chat")
    )
    llm_enabled: bool = field(default_factory=lambda: _b("LLM_ENABLED", True))

    # DISCORD_WEBHOOK_URL is accepted as an alias (common in deploy UIs).
    discord_webhook: str = field(
        default_factory=lambda: _s("DISCORD_WEBHOOK") or _s("DISCORD_WEBHOOK_URL")
    )
    discord_critical_webhook: str = field(
        default_factory=lambda: _s("DISCORD_CRITICAL_WEBHOOK")
    )

    # --- market data provider
    market_data_provider: str = field(
        default_factory=lambda: _s("MARKET_DATA_PROVIDER", "yahoo").lower()
    )
    alpaca_key_id: str = field(default_factory=lambda: _s("ALPACA_KEY_ID"))
    alpaca_secret_key: str = field(default_factory=lambda: _s("ALPACA_SECRET_KEY"))
    alpaca_feed: str = field(default_factory=lambda: _s("ALPACA_FEED", "iex").lower())
    # Free Basic options feed is "indicative". "opra" requires a signed
    # agreement and 403s on free tier — do not switch unless subscribed.
    alpaca_options_feed: str = field(
        default_factory=lambda: _s("ALPACA_OPTIONS_FEED", "indicative").lower()
    )
    # Soft ceiling (self-imposed). Must be ≤ hard venue limit (200/min).
    alpaca_rate_limit_per_min: int = field(
        default_factory=lambda: _i("ALPACA_RATE_LIMIT_PER_MIN", 100)
    )
    alpaca_hard_limit_per_min: int = field(
        default_factory=lambda: _i("ALPACA_HARD_LIMIT_PER_MIN", 200)
    )

    # --- news / sentiment agent. Rolling refresh; emits a float in [-1, 1].
    news_enabled: bool = field(default_factory=lambda: _b("NEWS_ENABLED", True))
    news_bias_weight: float = field(
        default_factory=lambda: _f("NEWS_BIAS_WEIGHT", 0.15)
    )
    # TTL must exceed the refresh interval or a working agent looks frozen.
    news_bias_ttl_hours: float = field(
        default_factory=lambda: _f("NEWS_BIAS_TTL_HOURS", 1.75)
    )
    news_refresh_interval_s: int = field(
        default_factory=lambda: _i("NEWS_REFRESH_INTERVAL_S", 1800)
    )

    # --- BTC multi-layer scalping (ATR-scaled R, not fixed percentages)
    scalp_enabled: bool = field(default_factory=lambda: _b("SCALP_ENABLED", True))
    scalp_atr_mult: float = field(default_factory=lambda: _f("SCALP_ATR_MULT", 1.5))
    scalp_target_r: float = field(default_factory=lambda: _f("SCALP_TARGET_R", 1.8))
    scalp_trail_arm_r: float = field(
        default_factory=lambda: _f("SCALP_TRAIL_ARM_R", 0.8)
    )
    scalp_trail_giveback_pct: float = field(
        default_factory=lambda: _f("SCALP_TRAIL_GIVEBACK_PCT", 0.35)
    )
    scalp_max_hold_min: float = field(
        default_factory=lambda: _f("SCALP_MAX_HOLD_MIN", 90.0)
    )
    scalp_vwap_period: int = field(default_factory=lambda: _i("SCALP_VWAP_PERIOD", 48))
    scalp_momentum_bars: int = field(
        default_factory=lambda: _i("SCALP_MOMENTUM_BARS", 6)
    )

    # --- opt-in outbound self-ping. Binds no port; keeps free-tier hosts awake.
    keepalive_enabled: bool = field(
        default_factory=lambda: _b("KEEPALIVE_ENABLED", False)
    )
    keepalive_interval_s: int = field(
        default_factory=lambda: _i("KEEPALIVE_INTERVAL_S", 300)
    )

    # --- switches
    trading_enabled: bool = field(default_factory=lambda: _b("TRADING_ENABLED", True))
    crypto_enabled: bool = field(default_factory=lambda: _b("CRYPTO_ENABLED", True))
    equity_enabled: bool = field(default_factory=lambda: _b("EQUITY_ENABLED", True))
    force_regime: str = field(default_factory=lambda: _s("FORCE_REGIME").upper())
    dry_run: bool = field(default_factory=lambda: _b("DRY_RUN", False))

    # ------------------------------------------------------------- validation
    def validate(self) -> list[str]:
        """Return a list of fatal problems. Empty list == safe to boot."""
        errs: list[str] = []

        if self.starting_capital <= 0:
            errs.append("STARTING_CAPITAL must be positive.")
        if not (0 < self.risk_pct_per_trade <= 0.25):
            errs.append("RISK_PCT_PER_TRADE must be between 0 and 0.25 (25%).")
        if self.max_open_positions < 1:
            errs.append("MAX_OPEN_POSITIONS must be at least 1.")
        for label, stop, target in (
            ("EQUITY", self.stop_loss_pct_equity, self.take_profit_pct_equity),
            ("CRYPTO", self.stop_loss_pct_crypto, self.take_profit_pct_crypto),
        ):
            if not (0 < stop < 1):
                errs.append(f"STOP_LOSS_PCT_{label} must be between 0 and 1.")
            if target <= 0:
                errs.append(f"TAKE_PROFIT_PCT_{label} must be positive.")
            if target <= stop:
                errs.append(
                    f"TAKE_PROFIT_PCT_{label} ({target}) must exceed "
                    f"STOP_LOSS_PCT_{label} ({stop}); otherwise every trade "
                    f"has negative expectancy by construction."
                )
        if self.min_trade_notional <= 0:
            errs.append("MIN_TRADE_NOTIONAL must be positive.")
        if not (0 <= self.daily_loss_limit_pct < 1):
            errs.append("DAILY_LOSS_LIMIT_PCT must be between 0 and 1.")
        if not self.equity_universe and self.equity_enabled:
            errs.append("EQUITY_UNIVERSE is empty but EQUITY_ENABLED is true.")
        if not self.crypto_universe and self.crypto_enabled:
            errs.append("CRYPTO_UNIVERSE is empty but CRYPTO_ENABLED is true.")
        if self.target_dte_min > self.target_dte_max:
            errs.append("TARGET_DTE_MIN cannot exceed TARGET_DTE_MAX.")
        if self.force_regime and self.force_regime not in {
            "EQUITY", "CRYPTO", "IDLE", "",
        }:
            errs.append("FORCE_REGIME must be EQUITY, CRYPTO, or IDLE.")

        # Ten-ticker universe lock.
        if (
            self.equity_enabled
            and not self.allow_custom_universe
            and set(self.equity_universe) != set(CANONICAL_EQUITY_UNIVERSE)
        ):
            errs.append(
                "EQUITY_UNIVERSE must be exactly the canonical ten-ticker set "
                f"({', '.join(CANONICAL_EQUITY_UNIVERSE)}). "
                "Set ALLOW_CUSTOM_UNIVERSE=true to override."
            )

        if not (0.0 <= self.news_bias_weight <= 0.5):
            errs.append("NEWS_BIAS_WEIGHT must be between 0 and 0.5.")
        if self.news_bias_ttl_hours <= 0:
            errs.append("NEWS_BIAS_TTL_HOURS must be positive.")
        if self.news_refresh_interval_s < 60:
            errs.append("NEWS_REFRESH_INTERVAL_S must be at least 60.")
        # TTL shorter than the refresh interval silently disables the agent:
        # every reading would be expired before the next refresh lands.
        ttl_s = self.news_bias_ttl_hours * 3600.0
        if ttl_s <= self.news_refresh_interval_s:
            errs.append(
                f"NEWS_BIAS_TTL_HOURS ({self.news_bias_ttl_hours}) must exceed "
                f"NEWS_REFRESH_INTERVAL_S ({self.news_refresh_interval_s}s) "
                f"or every reading expires before the next refresh."
            )

        if self.market_data_provider not in {"yahoo", "alpaca"}:
            errs.append("MARKET_DATA_PROVIDER must be 'yahoo' or 'alpaca'.")
        if self.market_data_provider == "alpaca":
            if not self.alpaca_key_id or not self.alpaca_secret_key:
                errs.append(
                    "ALPACA_KEY_ID and ALPACA_SECRET_KEY are required when "
                    "MARKET_DATA_PROVIDER=alpaca."
                )
        if self.alpaca_options_feed not in {"indicative", "opra"}:
            errs.append(
                "ALPACA_OPTIONS_FEED must be 'indicative' (free) or 'opra'."
            )
        if self.alpaca_rate_limit_per_min < 1:
            errs.append("ALPACA_RATE_LIMIT_PER_MIN must be at least 1.")
        if self.alpaca_rate_limit_per_min > self.alpaca_hard_limit_per_min:
            errs.append(
                f"ALPACA_RATE_LIMIT_PER_MIN ({self.alpaca_rate_limit_per_min}) "
                f"must be ≤ ALPACA_HARD_LIMIT_PER_MIN "
                f"({self.alpaca_hard_limit_per_min}). Soft ≤ hard."
            )

        if self.scalp_atr_mult <= 0:
            errs.append("SCALP_ATR_MULT must be positive.")
        if self.scalp_target_r <= 0:
            errs.append("SCALP_TARGET_R must be positive.")
        if self.scalp_trail_arm_r <= 0:
            errs.append("SCALP_TRAIL_ARM_R must be positive.")
        if self.scalp_max_hold_min <= 0:
            errs.append("SCALP_MAX_HOLD_MIN must be positive.")
        if self.scalp_vwap_period < 5:
            errs.append("SCALP_VWAP_PERIOD must be at least 5.")

        # Free-tier storage guard: refuse to boot without explicit ack when
        # the path looks like ephemeral Render storage (no /var/data mount).
        if self._looks_ephemeral() and not self.ephemeral_storage_ack:
            errs.append(
                "DB_PATH is on ephemeral storage and EPHEMERAL_STORAGE_ACK is "
                "not set. Free-tier hosts wipe the filesystem on restart and "
                "would silently lose the position book. Mount a disk (see "
                "render.paid.yaml) or set EPHEMERAL_STORAGE_ACK=true to "
                "acknowledge the risk."
            )

        # Budget arithmetic: one symbol's worth of calls must fit a scan slot.
        worst_symbol = self.data_call_budget_s * 3 + self.llm_chain_budget_s
        if worst_symbol > self.scan_interval_equity_s:
            errs.append(
                f"Per-symbol worst case ({worst_symbol:.0f}s) exceeds the equity "
                f"scan interval ({self.scan_interval_equity_s}s). Raise the "
                f"interval or lower the budgets."
            )
        return errs

    def _looks_ephemeral(self) -> bool:
        """
        True when running in a hosted environment that almost certainly has
        no persistent disk under the configured DB path.
        """
        if self.env not in {"production", "staging", "render"}:
            return False
        path = self.db_path
        # Paid blueprint mounts at /var/data. Anything else in production is
        # treated as ephemeral unless the operator has ack'd it.
        return not path.startswith("/var/data")

    # ------------------------------------------------------------- convenience
    def exit_params(self, market_value: str) -> dict[str, float]:
        """Exit-plan percentages for a market. One place, no duplication."""
        if market_value == "EQUITY_OPTION":
            return {
                "stop_pct": self.stop_loss_pct_equity,
                "target_pct": self.take_profit_pct_equity,
                "trail_activate_pct": self.trail_activate_pct_equity,
                "trail_giveback_pct": self.trail_giveback_pct_equity,
                "max_hold_hours": self.max_hold_hours_equity,
            }
        return {
            "stop_pct": self.stop_loss_pct_crypto,
            "target_pct": self.take_profit_pct_crypto,
            "trail_activate_pct": self.trail_activate_pct_crypto,
            "trail_giveback_pct": self.trail_giveback_pct_crypto,
            "max_hold_hours": self.max_hold_hours_crypto,
        }

    @property
    def llm_available(self) -> bool:
        return self.llm_enabled and bool(self.gemini_api_key or self.deepseek_api_key)

    @property
    def discord_available(self) -> bool:
        return bool(self.discord_webhook)

    def redacted(self) -> dict:
        """Safe-to-render view for the dashboard. Never leaks secrets."""
        def mask(v: str) -> str:
            return f"set ({len(v)} chars)" if v else "not set"

        return {
            "env": self.env,
            "dashboard_url": self.dashboard_url,
            "db_path": self.db_path,
            "starting_capital": self.starting_capital,
            "equity_universe": self.equity_universe,
            "crypto_universe": self.crypto_universe,
            "execute_threshold": self.execute_threshold,
            "risk_pct_per_trade": self.risk_pct_per_trade,
            "max_open_positions": self.max_open_positions,
            "max_positions_per_underlying": self.max_positions_per_underlying,
            "max_new_positions_per_day": self.max_new_positions_per_day,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "equity_exits": {
                "stop_pct": self.stop_loss_pct_equity,
                "target_pct": self.take_profit_pct_equity,
                "trail_at_pct": self.trail_activate_pct_equity,
                "giveback_pct": self.trail_giveback_pct_equity,
                "max_hold_h": self.max_hold_hours_equity,
            },
            "crypto_exits": {
                "stop_pct": self.stop_loss_pct_crypto,
                "target_pct": self.take_profit_pct_crypto,
                "trail_at_pct": self.trail_activate_pct_crypto,
                "giveback_pct": self.trail_giveback_pct_crypto,
                "max_hold_h": self.max_hold_hours_crypto,
            },
            "min_trade_notional": self.min_trade_notional,
            "reentry_cooldown_min": self.reentry_cooldown_min,
            "trading_enabled": self.trading_enabled,
            "equity_enabled": self.equity_enabled,
            "crypto_enabled": self.crypto_enabled,
            "dry_run": self.dry_run,
            "force_regime": self.force_regime or None,
            "gemini_api_key": mask(self.gemini_api_key),
            "deepseek_api_key": mask(self.deepseek_api_key),
            "discord_webhook": mask(self.discord_webhook),
            "news_enabled": self.news_enabled,
            "news_bias_weight": self.news_bias_weight,
            "news_bias_ttl_hours": self.news_bias_ttl_hours,
            "news_refresh_interval_s": self.news_refresh_interval_s,
            "market_data_provider": self.market_data_provider,
            "alpaca_key_id": mask(self.alpaca_key_id),
            "alpaca_secret_key": mask(self.alpaca_secret_key),
            "alpaca_feed": self.alpaca_feed,
            "alpaca_options_feed": self.alpaca_options_feed,
            "alpaca_rate_limit_per_min": self.alpaca_rate_limit_per_min,
            "scalp_enabled": self.scalp_enabled,
            "scalp_atr_mult": self.scalp_atr_mult,
            "scalp_target_r": self.scalp_target_r,
            "scalp_trail_arm_r": self.scalp_trail_arm_r,
            "scalp_max_hold_min": self.scalp_max_hold_min,
            "keepalive_enabled": self.keepalive_enabled,
            "ephemeral_storage_ack": self.ephemeral_storage_ack,
            "allow_custom_universe": self.allow_custom_universe,
        }


settings = Settings()


def assert_valid() -> None:
    """Call this at the top of every entrypoint. Refuses to run if broken."""
    errs = settings.validate()
    if errs:
        raise ConfigError(
            "Configuration is invalid; refusing to start.\n  - "
            + "\n  - ".join(errs)
        )
