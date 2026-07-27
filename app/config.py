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

    # --- web
    port: int = field(default_factory=lambda: _i("PORT", 8000))
    host: str = field(default_factory=lambda: _s("HOST", "0.0.0.0"))

    # --- capital
    starting_capital: float = field(
        default_factory=lambda: _f("STARTING_CAPITAL", 100_000.0)
    )

    # --- universes
    equity_universe: list[str] = field(
        default_factory=lambda: _list(
            "EQUITY_UNIVERSE",
            ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA",
             "AMZN", "META", "GOOGL", "TSLA"],
        )
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

    discord_webhook: str = field(default_factory=lambda: _s("DISCORD_WEBHOOK"))
    discord_critical_webhook: str = field(
        default_factory=lambda: _s("DISCORD_CRITICAL_WEBHOOK")
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

        # Budget arithmetic: one symbol's worth of calls must fit a scan slot.
        worst_symbol = self.data_call_budget_s * 3 + self.llm_chain_budget_s
        if worst_symbol > self.scan_interval_equity_s:
            errs.append(
                f"Per-symbol worst case ({worst_symbol:.0f}s) exceeds the equity "
                f"scan interval ({self.scan_interval_equity_s}s). Raise the "
                f"interval or lower the budgets."
            )
        return errs

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
