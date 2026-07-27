"""
Market adapters. One per desk shift.

The scanner does not know what an option is, and it does not know what a
satoshi is. It asks an adapter to `assess(symbol)` and gets back a
`SymbolAssessment` with a score, an instrument to trade, and a price. That is
the entire contract, and it is why adding a third desk - futures, FX,
whatever - is a new file here rather than a rewrite of the engine.

    EquityOptionsAdapter   09:30-16:00 ET, long calls and puts
    CryptoSpotAdapter      the rest of the time, long spot
"""
from __future__ import annotations

import logging
from typing import Protocol

from app.config import settings
from app.data.providers import (
    Bar,
    DataUnavailable,
    OptionQuote,
    Quote,
    crypto_provider,
    equity_provider,
)
from app.domain.models import Direction, Market, ScoreCard, SymbolAssessment, Verdict
from app.engine import indicators as ind
from app.engine import scoring

log = logging.getLogger("markets")


class MarketAdapter(Protocol):
    market: Market
    multiplier: float
    whole_units: bool

    def universe(self) -> list[str]: ...
    def assess(self, symbol: str, context: dict) -> SymbolAssessment: ...
    def build_context(self, symbols: list[str]) -> dict: ...
    def mark(self, instrument: str, underlying: str) -> float: ...


# ===========================================================================
# Equity options
# ===========================================================================
class EquityOptionsAdapter:
    market = Market.EQUITY_OPTION
    multiplier = 100.0
    whole_units = True

    def __init__(self) -> None:
        self.data = equity_provider()

    def universe(self) -> list[str]:
        return list(settings.equity_universe)

    def build_context(self, symbols: list[str]) -> dict:
        """
        Shared context fetched once per scan, not once per symbol.
        SPY is the relative-strength benchmark for the whole universe.
        """
        ctx: dict = {"benchmark": None, "benchmark_change_pct": None}
        try:
            spy = self.data.quote("SPY")
            ctx["benchmark"] = spy
            ctx["benchmark_change_pct"] = spy.change_pct
        except DataUnavailable as exc:
            log.warning("benchmark unavailable, relative strength neutral: %s", exc)
        return ctx

    # ------------------------------------------------------------ contract
    def _pick_contract(
        self, chain: list[OptionQuote], spot: float, atr_value: float, bullish: bool
    ) -> OptionQuote | None:
        """
        Choose a strike roughly one ATR beyond spot in the trade's direction -
        far enough to have gamma, close enough to have delta - then keep only
        contracts that clear the liquidity floor.
        """
        right = "C" if bullish else "P"
        target = spot + atr_value if bullish else spot - atr_value

        eligible = [
            q for q in chain
            if q.right == right
            and q.open_interest >= settings.min_open_interest
            and q.mid > 0.05
            and q.spread_pct <= settings.max_spread_pct
        ]
        if not eligible:
            return None
        return min(eligible, key=lambda q: abs(q.strike - target))

    # -------------------------------------------------------------- assess
    def assess(self, symbol: str, context: dict) -> SymbolAssessment:
        empty = ScoreCard(symbol=symbol)
        quote: Quote = self.data.quote(symbol)
        bars: list[Bar] = self.data.bars(symbol, lookback_days=60, interval="1d")

        if len(bars) < 25:
            return SymbolAssessment(
                symbol=symbol, market=self.market, score=empty,
                verdict=Verdict.ERROR, reason="not enough price history",
                ref_price=quote.price,
            )

        closes = [b.close for b in bars]
        atr_value = ind.atr(bars, 14) or (quote.price * 0.02)

        # Direction from trend, then score the setup on its own terms.
        bullish = ind.trend_score(closes) >= 0
        direction = Direction.LONG_CALL if bullish else Direction.LONG_PUT

        tech = scoring.score_technical(bars, bullish=bullish)
        sent = scoring.score_sentiment(
            change_pct=quote.change_pct,
            benchmark_change_pct=context.get("benchmark_change_pct"),
            momentum_pct=ind.momentum_pct(closes, 10),
            volume_ratio=ind.volume_ratio(bars, 20),
            bullish=bullish,
        )

        chain = self.data.option_chain(symbol, quote.price)
        contract = self._pick_contract(chain, quote.price, atr_value, bullish)
        if contract is None:
            card = scoring.compose(symbol, (0.0, {}), tech, sent)
            return SymbolAssessment(
                symbol=symbol, market=self.market, score=card,
                verdict=Verdict.PASS,
                reason="no contract cleared the liquidity floor",
                ref_price=quote.price, direction=direction, atr=atr_value,
            )

        liq = scoring.score_option_liquidity(contract)
        card = scoring.compose(symbol, liq, tech, sent)

        return SymbolAssessment(
            symbol=symbol,
            market=self.market,
            score=card,
            verdict=Verdict.EXECUTE if scoring.meets_threshold(card) else Verdict.PASS,
            reason=("cleared the threshold" if scoring.meets_threshold(card)
                    else f"scored {card.total:.1f}, needs "
                         f"{settings.execute_threshold:.0f}"),
            instrument=contract.contract,
            ref_price=quote.price,
            entry_price=contract.mid,
            direction=direction,
            atr=atr_value,
            detail={
                "expiry": contract.expiry,
                "strike": contract.strike,
                "right": contract.right,
                "bid": contract.bid,
                "ask": contract.ask,
                "open_interest": contract.open_interest,
                "implied_vol": contract.implied_vol,
                "spot": quote.price,
            },
        )

    def mark(self, instrument: str, underlying: str) -> float:
        """
        Re-price an open option.

        Yahoo's chain endpoint is the only free source, and it is slow. The
        chain is re-fetched and the exact contract located; if it is missing,
        the caller keeps the previous mark rather than acting on a guess.
        """
        quote = self.data.quote(underlying)
        chain = self.data.option_chain(underlying, quote.price)
        for q in chain:
            if q.contract == instrument:
                return q.mid
        raise DataUnavailable(f"{instrument} not found in the current chain")


# ===========================================================================
# Crypto spot
# ===========================================================================
class CryptoSpotAdapter:
    market = Market.CRYPTO_SPOT
    multiplier = 1.0
    whole_units = False

    def __init__(self) -> None:
        self.data = crypto_provider()

    def universe(self) -> list[str]:
        return list(settings.crypto_universe)

    def build_context(self, symbols: list[str]) -> dict:
        """BTC is the benchmark for everything else in crypto."""
        ctx: dict = {"benchmark_change_pct": None}
        try:
            btc = self.data.quote("BTC-USD")
            ctx["benchmark_change_pct"] = btc.change_pct
            ctx["btc_price"] = btc.price
        except DataUnavailable as exc:
            log.warning("BTC benchmark unavailable: %s", exc)
        return ctx

    def assess(self, symbol: str, context: dict) -> SymbolAssessment:
        empty = ScoreCard(symbol=symbol)
        quote = self.data.quote(symbol)
        bars = self.data.bars(symbol, lookback_days=14, interval="1h")

        if len(bars) < 30:
            return SymbolAssessment(
                symbol=symbol, market=self.market, score=empty,
                verdict=Verdict.ERROR, reason="not enough candle history",
                ref_price=quote.price,
            )

        closes = [b.close for b in bars]
        atr_value = ind.atr(bars, 14) or (quote.price * 0.01)

        # Spot-only for now, so no short side: a weak tape is a PASS, not a put.
        bullish = True
        benchmark = (None if symbol.upper() == "BTC-USD"
                     else context.get("benchmark_change_pct"))

        liq = scoring.score_spot_liquidity(bars)
        tech = scoring.score_technical(bars, bullish=True)
        sent = scoring.score_sentiment(
            change_pct=quote.change_pct,
            benchmark_change_pct=benchmark,
            momentum_pct=ind.momentum_pct(closes, 24),
            volume_ratio=ind.volume_ratio(bars, 24),
            bullish=True,
        )
        card = scoring.compose(symbol, liq, tech, sent)
        passes = scoring.meets_threshold(card)

        return SymbolAssessment(
            symbol=symbol,
            market=self.market,
            score=card,
            verdict=Verdict.EXECUTE if passes else Verdict.PASS,
            reason=("cleared the threshold" if passes
                    else f"scored {card.total:.1f}, needs "
                         f"{settings.execute_threshold:.0f}"),
            instrument=symbol.upper(),
            ref_price=quote.price,
            entry_price=quote.price,
            direction=Direction.LONG_SPOT,
            atr=atr_value,
            detail={
                "venue": quote.meta.get("venue"),
                "day_high": quote.day_high,
                "day_low": quote.day_low,
                "change_pct": round(quote.change_pct, 3),
                "atr_pct": round(ind.atr_pct(bars, 14) or 0, 4),
            },
        )

    def mark(self, instrument: str, underlying: str) -> float:
        return self.data.quote(instrument).price


# ------------------------------------------------------------------ factory
_adapters: dict[Market, MarketAdapter] = {}


def get_adapter(market: Market) -> MarketAdapter:
    if market not in _adapters:
        _adapters[market] = (
            EquityOptionsAdapter() if market is Market.EQUITY_OPTION
            else CryptoSpotAdapter()
        )
    return _adapters[market]


def adapter_for_regime(regime: str) -> MarketAdapter | None:
    if regime == "EQUITY":
        return get_adapter(Market.EQUITY_OPTION)
    if regime == "CRYPTO":
        return get_adapter(Market.CRYPTO_SPOT)
    return None
