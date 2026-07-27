"""Risk gates. Each one must block for its own stated reason."""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.db import repositories as repo
from app.domain.models import Direction, ExitPlan, Market, OrderIntent, Position
from app.engine import risk

KEY = "EQUITY_OPTION:NVDA:LONG_CALL:EQ-2026-07-24"


def call(**over):
    args = dict(
        market=Market.EQUITY_OPTION, underlying="NVDA",
        direction=Direction.LONG_CALL, idempotency_key=KEY,
        entry_price=5.00, multiplier=100,
    )
    args.update(over)
    return risk.check(**args)


def seed(symbol="NVDA", session="EQ-2026-07-24", price=5.00, qty=1):
    intent = OrderIntent(
        market=Market.EQUITY_OPTION, underlying=symbol,
        instrument=f"{symbol}260130C00150000", direction=Direction.LONG_CALL,
        quantity=qty, multiplier=100, limit_price=price, session_key=session,
        scan_id="s", score=75.0,
        plan=ExitPlan.build(price, stop_pct=.35, target_pct=.6,
                            trail_activate_pct=.3, trail_giveback_pct=.15,
                            max_hold_hours=48),
    )
    pos, _ = repo.positions.open_position(intent, price)
    repo.ledger.debit(price * qty * 100)
    return pos


def test_a_clean_signal_passes():
    d = call()
    assert d.allowed and d.max_notional > 0


def test_duplicate_is_blocked():
    seed()
    d = call()
    assert not d.allowed and d.gate == "DUPLICATE"


def test_per_underlying_cap():
    seed(session="EQ-2026-07-24")
    d = call(idempotency_key="EQUITY_OPTION:NVDA:LONG_CALL:EQ-2026-07-25")
    assert not d.allowed and d.gate == "PER_UNDERLYING_CAP"


def test_max_open_positions():
    for i in range(settings.max_open_positions):
        seed(symbol=f"SYM{i}")
    d = call()
    assert not d.allowed and d.gate == "MAX_OPEN"


def test_cooldown_after_a_close():
    pos = seed()
    repo.positions.close(pos.position_id, 6.00, "TAKE_PROFIT")
    d = call(idempotency_key="EQUITY_OPTION:NVDA:LONG_CALL:EQ-2026-07-25")
    assert not d.allowed and d.gate == "COOLDOWN"
    assert "cooldown" in d.reason


def test_position_too_large_for_the_per_trade_budget():
    # 2% of 100k = 2,000. One contract at $50 costs 5,000.
    d = call(entry_price=50.00)
    assert not d.allowed and d.gate == "SIZE_TOO_LARGE"


def test_halt_blocks_everything():
    repo.kv.set("halted", "true")
    repo.kv.set("halt_reason", "testing the switch")
    d = call()
    assert not d.allowed and d.gate == "HALTED"
    assert d.reason == "testing the switch"


def test_daily_loss_limit():
    pos = seed(price=15.00)
    # Close it at a heavy loss: (0.10 - 15) * 100 = -1,490 ... run it twice
    repo.positions.close(pos.position_id, 0.10, "STOP_LOSS")
    for i in range(4):
        p = seed(symbol=f"L{i}", price=15.00)
        repo.positions.close(p.position_id, 0.10, "STOP_LOSS")

    d = call(idempotency_key="EQUITY_OPTION:ZZZZ:LONG_CALL:EQ-2026-07-24",
             underlying="ZZZZ")
    assert not d.allowed
    assert d.gate in {"DAILY_LOSS_LIMIT", "DAILY_TRADE_CAP"}


# ------------------------------------------------------------------ sizing
def test_options_size_to_whole_contracts():
    assert risk.size_position(2000, 5.00, 100, whole_units=True) == 4.0
    assert risk.size_position(450, 5.00, 100, whole_units=True) == 0.0


def test_crypto_sizes_fractionally():
    qty = risk.size_position(2000, 68_500.0, 1, whole_units=False)
    assert 0.029 < qty < 0.030


def test_zero_price_sizes_to_zero():
    assert risk.size_position(1000, 0.0, 100, whole_units=True) == 0.0


# ---------------------------------------------------------------- summary
def test_portfolio_summary_shape():
    s = risk.portfolio_summary()
    for key in ("cash", "equity", "realized_pnl", "unrealized_pnl",
                "open_count", "drawdown_pct", "halted"):
        assert key in s
    assert s["equity"] == pytest.approx(settings.starting_capital)
