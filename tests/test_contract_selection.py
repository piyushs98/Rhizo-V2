"""Option strike targeting via TARGET_MONEYNESS — no network."""
from __future__ import annotations

from dataclasses import replace

import app.markets.adapters as adapters_mod
from app.config import settings
from app.data.providers import OptionQuote
from app.markets.adapters import EquityOptionsAdapter


def _chain(spot: float = 100.0) -> list[OptionQuote]:
    """ATM + OTM calls/puts with enough depth/spread to clear floors."""
    out = []
    for k in (100.0, 102.0, 105.0, 110.0, 95.0, 90.0):
        mid = max(0.10, 5.0 - abs(k - spot) * 0.4)
        half = mid * 0.02
        out.append(OptionQuote(
            contract=f"T{int(k)}C", underlying="TEST", expiry="2026-08-21",
            strike=k, right="C",
            bid=mid - half, ask=mid + half, last=mid,
            volume=500, open_interest=1000,
        ))
        out.append(OptionQuote(
            contract=f"T{int(k)}P", underlying="TEST", expiry="2026-08-21",
            strike=k, right="P",
            bid=mid - half, ask=mid + half, last=mid,
            volume=500, open_interest=1000,
        ))
    return out


def _adapter_with(*, moneyness: float, min_oi: int | None = None,
                  max_spread: float | None = None) -> EquityOptionsAdapter:
    kw = {"target_moneyness": moneyness}
    if min_oi is not None:
        kw["min_open_interest"] = min_oi
    if max_spread is not None:
        kw["max_spread_pct"] = max_spread
    adapters_mod.settings = replace(settings, **kw)
    return EquityOptionsAdapter()


def teardown_function() -> None:
    """Restore the real settings singleton after moneyness patches."""
    adapters_mod.settings = settings


def test_default_moneyness_picks_atm_call():
    adapter = _adapter_with(moneyness=0.0)
    c = adapter._pick_contract(_chain(100.0), spot=100.0, atr_value=2.0, bullish=True)
    assert c is not None
    assert c.strike == 100.0
    assert c.right == "C"


def test_five_pct_otm_shifts_call_strike():
    adapter = _adapter_with(moneyness=0.05)
    c = adapter._pick_contract(_chain(100.0), spot=100.0, atr_value=2.0, bullish=True)
    assert c is not None
    assert c.strike == 105.0


def test_ten_pct_otm_call():
    adapter = _adapter_with(moneyness=0.10)
    c = adapter._pick_contract(_chain(100.0), spot=100.0, atr_value=2.0, bullish=True)
    assert c is not None
    assert c.strike == 110.0


def test_otm_put_targets_below_spot():
    adapter = _adapter_with(moneyness=0.05)
    c = adapter._pick_contract(_chain(100.0), spot=100.0, atr_value=2.0, bullish=False)
    assert c is not None
    assert c.right == "P"
    assert c.strike == 95.0


def test_liquidity_floor_not_relaxed_for_otm():
    """Penny-wide-spread OTM contracts must not become eligible just because we aim OTM."""
    adapter = _adapter_with(moneyness=0.10, min_oi=250, max_spread=0.12)
    chain = [
        OptionQuote(
            contract="CHEAP", underlying="TEST", expiry="2026-08-21",
            strike=110.0, right="C",
            bid=0.01, ask=0.10, last=0.05,
            volume=1, open_interest=5,
        ),
    ]
    assert adapter._pick_contract(chain, spot=100.0, atr_value=1.0, bullish=True) is None
