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
    # One contract at $50 costs 5,000 — above MAX_SINGLE_TRADE_PCT of equity.
    d = call(entry_price=50.00)
    assert not d.allowed
    assert d.gate in {"SIZE_TOO_LARGE", "MAX_SINGLE_TRADE"}


def test_options_raises_budget_to_afford_one_contract_under_cap():
    # At $2k, 2% = $40. One contract at $3 mid costs $300 (15% of equity),
    # under the 25% hard cap → budget is raised to afford exactly 1.
    d = call(entry_price=3.00)
    assert d.allowed
    assert d.max_notional == pytest.approx(300.0)


def test_options_hard_cap_blocks_half_account_contract():
    # $8 mid → $800 = 40% of $2k equity → refuse even if we could raise risk.
    d = call(entry_price=8.00)
    assert not d.allowed and d.gate == "MAX_SINGLE_TRADE"


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
                "open_count", "drawdown_pct", "halted", "crypto_allocation",
                "equity_instrument"):
        assert key in s
    assert s["equity"] == pytest.approx(settings.starting_capital)


def test_crypto_bucket_is_ring_fenced():
    """Crypto sizes against CRYPTO_ALLOCATION, not the full account."""
    d = risk.check(
        market=Market.CRYPTO_SPOT, underlying="BTC-USD",
        direction=Direction.LONG_SPOT,
        idempotency_key="CRYPTO_SPOT:BTC-USD:LONG_SPOT:CX-test",
        entry_price=50_000.0, multiplier=1.0, whole_units=False,
    )
    # 2% of $100 allocation = $2, below MIN_TRADE_NOTIONAL → blocked
    assert not d.allowed
    assert d.gate in {"SIZE_TOO_LARGE", "INSUFFICIENT_CASH"}
    assert "crypto" in d.reason.lower()
