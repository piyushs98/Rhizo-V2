"""
Market adapters. One per desk shift.

The scanner does not know what an option is, and it does not know what a
satoshi is. It asks an adapter to `assess(symbol)` and gets back a
`SymbolAssessment` with a score, an instrument to trade, and a price.

Equity path is batched: `build_context()` fetches quotes and bars for the
whole universe in one call each; `assess()` is network-free except a
deferred option chain gated by an exact bound.
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
from app.engine import scalping

log = logging.getLogger("markets")


class MarketAdapter(Protocol):
    market: Market
    multiplier: float
    whole_units: bool

    def universe(self) -> list[str]: ...
    def assess(self, symbol: str, context: dict) -> SymbolAssessment: ...
    def build_context(self, symbols: list[str]) -> dict: ...
    def mark(self, instrument: str, underlying: str) -> float: ...
    def mark_many(
        self, items: list[tuple[str, str]]
    ) -> dict[str, float]: ...
    def recent_bars(self, underlying: str) -> list[Bar]: ...


def _chain_worth_fetching(
    tech: tuple[float, dict],
    sent: tuple[float, dict],
    weights: dict[str, float] | None = None,
) -> bool:
    """
    Exact bound: award liquidity a perfect 100. If the composed total still
    cannot clear EXECUTE_THRESHOLD, skip the chain lookup. Being a maximum
    rather than an estimate, this never discards a candidate that would have
    qualified.
    """
    card = scoring.compose("_bound", (100.0, {}), tech, sent, weights=weights)
    return scoring.meets_threshold(card)


# ===========================================================================
# Equity options
# ===========================================================================
class EquityOptionsAdapter:
    market = Market.EQUITY_OPTION
    multiplier = 100.0
    whole_units = True

    def __init__(self) -> None:
        self.data = equity_provider()
        self._last_context_requests = 0
        self._chains_fetched = 0

    def universe(self) -> list[str]:
        return list(settings.equity_universe)

    def build_context(self, symbols: list[str]) -> dict:
        """
        Batched fetch once per scan. Quotes + bars for the whole universe.
        assess() then reads from context maps — no per-symbol network.
        """
        ctx: dict = {
            "benchmark": None,
            "benchmark_change_pct": None,
            "news_bias": 0.0,
            "quotes": {},
            "bars": {},
            "requests": 0,
            "provider": getattr(self.data, "name", "unknown"),
        }
        try:
            from app.agents import news as news_agent
            ctx["news_bias"] = news_agent.current_bias("MACRO")
        except Exception as exc:
            log.warning("news bias unavailable, neutral: %s", exc)

        symbols = list(dict.fromkeys([*(s.upper() for s in symbols), "SPY"]))
        req_before = getattr(self.data, "request_count", None)

        if hasattr(self.data, "quotes_many") and hasattr(self.data, "bars_many"):
            try:
                ctx["quotes"] = self.data.quotes_many(symbols)
            except DataUnavailable as exc:
                log.warning("batched quotes failed: %s", exc)
            try:
                ctx["bars"] = self.data.bars_many(
                    symbols, lookback_days=60, interval="1d"
                )
            except DataUnavailable as exc:
                log.warning("batched bars failed: %s", exc)
        else:
            # Yahoo path: per-symbol (legacy footprint).
            for sym in symbols:
                try:
                    ctx["quotes"][sym] = self.data.quote(sym)
                except DataUnavailable:
                    pass
                try:
                    ctx["bars"][sym] = self.data.bars(
                        sym, lookback_days=60, interval="1d"
                    )
                except DataUnavailable:
                    pass

        # Enrich quote.prev_close from bars when the batch quote lacks it.
        for sym, q in list(ctx["quotes"].items()):
            bars = ctx["bars"].get(sym) or []
            if q.prev_close is None and len(bars) >= 2:
                ctx["quotes"][sym] = Quote(
                    symbol=q.symbol,
                    price=q.price,
                    ts=q.ts,
                    prev_close=bars[-2].close,
                    day_high=q.day_high or bars[-1].high,
                    day_low=q.day_low or bars[-1].low,
                    volume=q.volume if q.volume is not None else bars[-1].volume,
                    meta=q.meta,
                )

        spy = ctx["quotes"].get("SPY")
        if spy is not None:
            ctx["benchmark"] = spy
            ctx["benchmark_change_pct"] = spy.change_pct

        req_after = getattr(self.data, "request_count", None)
        if req_before is not None and req_after is not None:
            ctx["requests"] = max(0, req_after - req_before)
        self._last_context_requests = ctx["requests"]
        self._chains_fetched = 0
        return ctx

    def _pick_contract(
        self, chain: list[OptionQuote], spot: float, atr_value: float, bullish: bool
    ) -> OptionQuote | None:
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

    def assess(self, symbol: str, context: dict) -> SymbolAssessment:
        empty = ScoreCard(symbol=symbol)
        sym = symbol.upper()
        quotes: dict = context.get("quotes") or {}
        bars_map: dict = context.get("bars") or {}

        quote = quotes.get(sym)
        bars = bars_map.get(sym) or []

        # Fallback only if context was incomplete (should be rare).
        if quote is None:
            quote = self.data.quote(sym)
        if len(bars) < 25:
            try:
                bars = self.data.bars(sym, lookback_days=60, interval="1d")
            except DataUnavailable:
                bars = []

        if len(bars) < 25:
            return SymbolAssessment(
                symbol=symbol, market=self.market, score=empty,
                verdict=Verdict.ERROR, reason="not enough price history",
                ref_price=quote.price if quote else None,
            )

        closes = [b.close for b in bars]
        atr_value = ind.atr(bars, 14) or (quote.price * 0.02)

        bullish = ind.trend_score(closes) >= 0
        direction = Direction.LONG_CALL if bullish else Direction.LONG_PUT

        tech = scoring.score_technical(bars, bullish=bullish)
        news_bias = float(context.get("news_bias") or 0.0)
        sent = scoring.score_sentiment(
            change_pct=quote.change_pct,
            benchmark_change_pct=context.get("benchmark_change_pct"),
            momentum_pct=ind.momentum_pct(closes, 10),
            volume_ratio=ind.volume_ratio(bars, 20),
            bullish=bullish,
            news_bias=news_bias,
        )

        # Exact bound: skip chain when even perfect liquidity cannot execute.
        if not _chain_worth_fetching(tech, sent):
            card = scoring.compose(symbol, (0.0, {"skipped": 1.0}), tech, sent)
            return SymbolAssessment(
                symbol=symbol, market=self.market, score=card,
                verdict=Verdict.PASS,
                reason="chain skipped: cannot clear threshold even at perfect liquidity",
                ref_price=quote.price, direction=direction, atr=atr_value,
                detail={"chain_fetched": False},
            )

        chain = self.data.option_chain(symbol, quote.price)
        self._chains_fetched += 1
        contract = self._pick_contract(chain, quote.price, atr_value, bullish)
        if contract is None:
            card = scoring.compose(symbol, (0.0, {}), tech, sent)
            return SymbolAssessment(
                symbol=symbol, market=self.market, score=card,
                verdict=Verdict.PASS,
                reason="no contract cleared the liquidity floor",
                ref_price=quote.price, direction=direction, atr=atr_value,
                detail={"chain_fetched": True},
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
                "chain_fetched": True,
            },
        )

    def mark(self, instrument: str, underlying: str) -> float:
        if hasattr(self.data, "mark_options"):
            marks = self.data.mark_options([instrument])
            if instrument in marks:
                return marks[instrument]
        quote = self.data.quote(underlying)
        chain = self.data.option_chain(underlying, quote.price)
        for q in chain:
            if q.contract == instrument:
                return q.mid
        raise DataUnavailable(f"{instrument} not found in the current chain")

    def mark_many(self, items: list[tuple[str, str]]) -> dict[str, float]:
        """items: list of (instrument, underlying). Returns instrument -> mid."""
        if not items:
            return {}
        contracts = [inst for inst, _ in items]
        if hasattr(self.data, "mark_options"):
            try:
                return self.data.mark_options(contracts)
            except DataUnavailable as exc:
                log.warning("batched option marks failed: %s", exc)
        out: dict[str, float] = {}
        for inst, und in items:
            try:
                out[inst] = self.mark(inst, und)
            except DataUnavailable:
                continue
        return out

    def recent_bars(self, underlying: str) -> list[Bar]:
        return self.data.bars(underlying, lookback_days=60, interval="1d")


# ===========================================================================
# Crypto spot
# ===========================================================================
class CryptoSpotAdapter:
    market = Market.CRYPTO_SPOT
    multiplier = 1.0
    whole_units = False

    def __init__(self) -> None:
        self.data = crypto_provider()
        self._last_context_requests = 0
        self._chains_fetched = 0

    def universe(self) -> list[str]:
        return list(settings.crypto_universe)

    def build_context(self, symbols: list[str]) -> dict:
        ctx: dict = {
            "benchmark_change_pct": None,
            "news_bias": 0.0,
            "quotes": {},
            "bars": {},
            "requests": 0,
        }
        try:
            from app.agents import news as news_agent
            ctx["news_bias"] = news_agent.current_bias("CRYPTO")
        except Exception as exc:
            log.warning("crypto news bias unavailable: %s", exc)

        # Crypto venues are not multi-symbol batched the same way; still
        # fetch once per symbol into context so assess() is network-free.
        for sym in symbols:
            try:
                ctx["quotes"][sym.upper()] = self.data.quote(sym)
                ctx["requests"] += 1
            except DataUnavailable as exc:
                log.warning("crypto quote %s: %s", sym, exc)
            try:
                ctx["bars"][sym.upper()] = self.data.bars(
                    sym, lookback_days=14, interval="1h"
                )
                ctx["requests"] += 1
            except DataUnavailable as exc:
                log.warning("crypto bars %s: %s", sym, exc)

        btc = ctx["quotes"].get("BTC-USD")
        if btc is not None:
            ctx["benchmark_change_pct"] = btc.change_pct
            ctx["btc_price"] = btc.price
        self._last_context_requests = ctx["requests"]
        return ctx

    def assess(self, symbol: str, context: dict) -> SymbolAssessment:
        empty = ScoreCard(symbol=symbol)
        sym = symbol.upper()
        quote = (context.get("quotes") or {}).get(sym)
        bars = (context.get("bars") or {}).get(sym) or []

        if quote is None:
            quote = self.data.quote(symbol)
        if len(bars) < 30:
            try:
                bars = self.data.bars(symbol, lookback_days=14, interval="1h")
            except DataUnavailable:
                bars = []

        if len(bars) < 30:
            return SymbolAssessment(
                symbol=symbol, market=self.market, score=empty,
                verdict=Verdict.ERROR, reason="not enough candle history",
                ref_price=quote.price if quote else None,
            )

        closes = [b.close for b in bars]
        atr_value = ind.atr(bars, 14) or (quote.price * 0.01)

        benchmark = (None if sym == "BTC-USD"
                     else context.get("benchmark_change_pct"))

        liq = scoring.score_spot_liquidity(bars)
        tech = scoring.score_technical(bars, bullish=True)
        news_bias = float(context.get("news_bias") or 0.0)
        sent = scoring.score_sentiment(
            change_pct=quote.change_pct,
            benchmark_change_pct=benchmark,
            momentum_pct=ind.momentum_pct(closes, 24),
            volume_ratio=ind.volume_ratio(bars, 24),
            bullish=True,
            news_bias=news_bias,
        )
        card = scoring.compose(symbol, liq, tech, sent)
        passes = scoring.meets_threshold(card)

        exit_plan = None
        reason = ("cleared the threshold" if passes
                  else f"scored {card.total:.1f}, needs "
                       f"{settings.execute_threshold:.0f}")
        verdict = Verdict.EXECUTE if passes else Verdict.PASS

        is_btc = sym in {"BTC-USD", "BTC", "BTCUSD"}
        detail_extra: dict = {}
        if is_btc and settings.scalp_enabled:
            gate_ok, gate_diag = scalping.entry_gate(bars)
            detail_extra = {f"scalp.{k}": v for k, v in gate_diag.items()}
            if not gate_ok:
                verdict = Verdict.PASS
                reason = "scalp entry gate failed (VWAP/momentum)"
                exit_plan = None
            elif passes:
                exit_plan = scalping.build_plan(quote.price, bars)
                if exit_plan is None:
                    verdict = Verdict.PASS
                    reason = "could not build ATR scalp plan"
                else:
                    reason = "scalp gate + score cleared"

        return SymbolAssessment(
            symbol=symbol,
            market=self.market,
            score=card,
            verdict=verdict,
            reason=reason,
            instrument=sym,
            ref_price=quote.price,
            entry_price=quote.price,
            direction=Direction.LONG_SPOT,
            atr=atr_value,
            exit_plan=exit_plan,
            detail={
                "venue": quote.meta.get("venue"),
                "day_high": quote.day_high,
                "day_low": quote.day_low,
                "change_pct": round(quote.change_pct, 3),
                "atr_pct": round(ind.atr_pct(bars, 14) or 0, 4),
                "chain_fetched": False,
                **detail_extra,
            },
        )

    def mark(self, instrument: str, underlying: str) -> float:
        return self.data.quote(instrument).price

    def mark_many(self, items: list[tuple[str, str]]) -> dict[str, float]:
        out: dict[str, float] = {}
        # Dedupe instruments; one quote each.
        seen: set[str] = set()
        for inst, _ in items:
            if inst in seen:
                continue
            seen.add(inst)
            try:
                out[inst] = self.data.quote(inst).price
            except DataUnavailable:
                continue
        return out

    def recent_bars(self, underlying: str) -> list[Bar]:
        return self.data.bars(underlying, lookback_days=14, interval="1h")


# ------------------------------------------------------------------ factory
_adapters: dict[Market, MarketAdapter] = {}


def get_adapter(market: Market) -> MarketAdapter:
    if market not in _adapters:
        _adapters[market] = (
            EquityOptionsAdapter() if market is Market.EQUITY_OPTION
            else CryptoSpotAdapter()
        )
    return _adapters[market]


def reset_adapters() -> None:
    _adapters.clear()


def adapter_for_regime(regime: str) -> MarketAdapter | None:
    if regime == "EQUITY":
        return get_adapter(Market.EQUITY_OPTION)
    if regime == "CRYPTO":
        return get_adapter(Market.CRYPTO_SPOT)
    return None
