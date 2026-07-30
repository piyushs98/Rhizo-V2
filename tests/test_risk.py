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


def seed(symbol="NVDA", session="EQ-2026-07-24", price=5.00, qty=1,
         market=Market.EQUITY_OPTION, direction=Direction.LONG_CALL,
         multiplier=100.0):
    intent = OrderIntent(
        market=market, underlying=symbol,
        instrument=f"{symbol}260130C00150000" if market is Market.EQUITY_OPTION
        else symbol,
        direction=direction,
        quantity=qty, multiplier=multiplier, limit_price=price,
        session_key=session,
        scan_id="s", score=75.0,
        plan=ExitPlan.build(price, stop_pct=.35, target_pct=.6,
                            trail_activate_pct=.3, trail_giveback_pct=.15,
                            max_hold_hours=48),
    )
    pos, _ = repo.positions.open_position(intent, price)
    repo.ledger.debit(price * qty * multiplier)
    return pos


def seed_crypto(notional: float, session: str = "CX-2026-07-24",
                symbol: str = "BTC-USD", price: float = 50_000.0):
    qty = notional / price
    return seed(
        symbol=symbol, session=session, price=price, qty=qty,
        market=Market.CRYPTO_SPOT, direction=Direction.LONG_SPOT,
        multiplier=1.0,
    )


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
    # Cost $400 under MAX_SINGLE_TRADE_PCT — allowed (budget or floor-raise).
    d = call(entry_price=4.00)
    assert d.allowed
    assert d.max_notional >= 400.0 - 1e-6


def test_options_hard_cap_blocks_half_account_contract():
    # $30 mid → $3,000. At $10k that is 30% > 25% hard cap.
    d = call(entry_price=30.00)
    assert not d.allowed and d.gate == "MAX_SINGLE_TRADE"


def test_halt_blocks_everything():
    repo.kv.set("halted", "true")
    repo.kv.set("halt_reason", "testing the switch")
    d = call()
    assert not d.allowed and d.gate == "HALTED"
    assert d.reason == "testing the switch"


def test_daily_loss_limit():
    pos = seed(price=15.00)
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
                "open_count", "drawdown_pct", "halted",
                "open_equity_notional", "open_crypto_notional",
                "crypto_max_exposure", "crypto_headroom",
                "equity_instrument"):
        assert key in s
    assert s["equity"] == pytest.approx(settings.starting_capital)
    assert s["crypto_max_exposure"] == pytest.approx(settings.crypto_max_exposure)
    assert s["crypto_headroom"] == pytest.approx(settings.crypto_max_exposure)


def test_crypto_exposure_cap_blocks_over_cap_entry():
    """
    Concurrent open crypto MTM cannot exceed CRYPTO_MAX_EXPOSURE.
    Five sequential closed scalps are fine; concurrent over-cap is not.
    """
    cap = settings.crypto_max_exposure
    # Fill almost the entire cap with one open crypto position.
    seed_crypto(cap - 10.0, session="CX-cap-1", symbol="BTC-USD")
    # Another crypto ticket of min notional would breach the cap.
    d = risk.check(
        market=Market.CRYPTO_SPOT, underlying="ETH-USD",
        direction=Direction.LONG_SPOT,
        idempotency_key="CRYPTO_SPOT:ETH-USD:LONG_SPOT:CX-cap-2",
        entry_price=3_000.0, multiplier=1.0, whole_units=False,
    )
    assert not d.allowed
    assert d.gate == "CRYPTO_EXPOSURE_CAP"
    assert "headroom" in d.reason.lower() or "cap" in d.reason.lower()


def test_crypto_exposure_cap_allows_within_headroom():
    """Room under the cap + shared pool cash → crypto entry allowed."""
    d = risk.check(
        market=Market.CRYPTO_SPOT, underlying="BTC-USD",
        direction=Direction.LONG_SPOT,
        idempotency_key="CRYPTO_SPOT:BTC-USD:LONG_SPOT:CX-ok",
        entry_price=50_000.0, multiplier=1.0, whole_units=False,
    )
    assert d.allowed
    # Budget limited by risk_pct and by crypto headroom.
    assert d.max_notional > 0
    assert d.max_notional <= settings.crypto_max_exposure + 1e-6


def test_crypto_insufficient_cash_distinct_from_cap():
    """
    When equity holds most of the cash, crypto is blocked by
    INSUFFICIENT_CASH — not CRYPTO_EXPOSURE_CAP — so the board can tell
    'equity ate the account' from 'crypto was full'.
    """
    # Drain cash into equity (leave a free open-slot so MAX_OPEN is not hit).
    # 4 × $2,499 = $9,996 under the $2,500 single-trade hard cap → cash $4.
    n = max(1, settings.max_open_positions - 1)
    for i in range(n):
        seed(symbol=f"EQ{i}", price=24.99, qty=1)
    assert repo.ledger.get()["cash"] < settings.min_trade_notional

    d = risk.check(
        market=Market.CRYPTO_SPOT, underlying="BTC-USD",
        direction=Direction.LONG_SPOT,
        idempotency_key="CRYPTO_SPOT:BTC-USD:LONG_SPOT:CX-starved",
        entry_price=50_000.0, multiplier=1.0, whole_units=False,
    )
    assert not d.allowed
    assert d.gate == "INSUFFICIENT_CASH"
    assert "equity ate" in d.reason.lower() or "open equity" in d.reason.lower()
    assert d.gate != "CRYPTO_EXPOSURE_CAP"


def test_closed_crypto_does_not_consume_exposure_cap():
    """Closed scalps free headroom — cap is open MTM only."""
    pos = seed_crypto(settings.crypto_max_exposure - 5.0,
                      session="CX-seq-1", symbol="BTC-USD")
    # Close it — exposure should free. Re-enter a *different* underlying so
    # re-entry cooldown on BTC does not confuse the assertion.
    repo.positions.close(pos.position_id, 50_000.0, "TAKE_PROFIT")
    repo.ledger.credit(settings.crypto_max_exposure - 5.0, 0.0, 0.0)

    d = risk.check(
        market=Market.CRYPTO_SPOT, underlying="ETH-USD",
        direction=Direction.LONG_SPOT,
        idempotency_key="CRYPTO_SPOT:ETH-USD:LONG_SPOT:CX-seq-2",
        entry_price=3_000.0, multiplier=1.0, whole_units=False,
    )
    assert d.allowed
    assert d.max_notional > 0
