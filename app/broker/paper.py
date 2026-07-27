"""
Paper broker. Simulated fills against the virtual ledger.

Two honesty features v1 lacked:

  - Fills cross the spread. Buying at the ask and selling at the bid is what
    actually happens, and a backtest that ignores it will flatter a strategy
    into looking profitable when it is not.
  - Fees are modelled explicitly, per market.

Both are configurable, and both are recorded so realized PnL is comparable
to what a live account would have produced.
"""
from __future__ import annotations

import logging

from app.broker.base import Fill
from app.db import repositories as repo
from app.domain.models import Market, OrderIntent, Position

log = logging.getLogger("broker")

# Assumed cost model. Tune these to your venue before trusting the PnL.
SLIPPAGE_PCT = {Market.EQUITY_OPTION: 0.010, Market.CRYPTO_SPOT: 0.0008}
FEE_PER_CONTRACT = 0.65      # typical retail options commission
CRYPTO_FEE_PCT = 0.0060      # taker fee on a retail tier


class PaperBroker:
    name = "paper"

    def buy(self, intent: OrderIntent) -> Fill:
        slip = SLIPPAGE_PCT.get(intent.market, 0.0)
        fill_price = round(intent.limit_price * (1 + slip), 6)
        gross = fill_price * intent.quantity * intent.multiplier

        if intent.market is Market.EQUITY_OPTION:
            fees = round(FEE_PER_CONTRACT * intent.quantity, 2)
        else:
            fees = round(gross * CRYPTO_FEE_PCT, 2)

        repo.ledger.debit(gross, fees)
        log.info(
            "paper buy %s x%s @ %.4f (ref %.4f, fees %.2f)",
            intent.instrument, intent.quantity, fill_price,
            intent.limit_price, fees,
        )
        return Fill(
            price=fill_price,
            quantity=intent.quantity,
            fees=fees,
            slippage=round((fill_price - intent.limit_price)
                           * intent.quantity * intent.multiplier, 2),
            venue="paper",
        )

    def sell(self, position: Position, price: float, reason: str) -> Fill:
        slip = SLIPPAGE_PCT.get(position.market, 0.0)
        fill_price = round(max(price * (1 - slip), 0.0), 6)
        gross = fill_price * position.quantity * position.multiplier

        if position.market is Market.EQUITY_OPTION:
            fees = round(FEE_PER_CONTRACT * position.quantity, 2)
        else:
            fees = round(gross * CRYPTO_FEE_PCT, 2)

        realized = round(
            (fill_price - (position.entry_price or 0.0))
            * position.quantity * position.multiplier - fees, 2
        )
        repo.ledger.credit(gross, realized, fees)
        log.info(
            "paper sell %s x%s @ %.4f (%s) pnl %.2f",
            position.instrument, position.quantity, fill_price, reason, realized,
        )
        return Fill(
            price=fill_price,
            quantity=position.quantity,
            fees=fees,
            slippage=round((price - fill_price)
                           * position.quantity * position.multiplier, 2),
            venue="paper",
            note=reason,
        )
