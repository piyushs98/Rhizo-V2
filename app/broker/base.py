"""
Broker interface.

Everything above this line is broker-agnostic. Wiring a real venue later
means implementing `Broker` against Alpaca / IBKR / Tradier / Coinbase and
changing one line in the engine factory. The order model already carries the
fields a real fill needs (submitted vs filled price, fees, slippage) so the
abstraction does not have to be redesigned to accommodate it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.models import OrderIntent, Position


@dataclass
class Fill:
    price: float
    quantity: float
    fees: float = 0.0
    slippage: float = 0.0
    venue: str = "paper"
    note: str = ""


class Broker(Protocol):
    name: str

    def buy(self, intent: OrderIntent) -> Fill: ...
    def sell(self, position: Position, price: float, reason: str) -> Fill: ...
