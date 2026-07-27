"""
Market data providers behind one interface.

Two things matter here:

  1. Everything the scanner needs is expressed as `Quote`, `Bar`, and
     `OptionQuote`. Swapping Yahoo for Polygon or Coinbase for Kraken is a
     change in this file and nowhere else.
  2. Crypto uses a provider *chain* - Coinbase first, Kraken on failure -
     exactly like the LLM chain. Neither venue is a single point of failure.

Everything is wrapped in a circuit breaker and a wall-clock budget.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.config import settings
from app.data.http import SESSION, pace
from app.resilience.circuit_breaker import BreakerOpen, get_breaker
from app.resilience.timeouts import CallTimeout, budget

log = logging.getLogger("data")


class DataUnavailable(RuntimeError):
    """The provider could not answer. Caller should skip this symbol."""


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Quote:
    symbol: str
    price: float
    ts: datetime
    prev_close: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def change_pct(self) -> float:
        if not self.prev_close:
            return 0.0
        return (self.price - self.prev_close) / self.prev_close * 100.0


@dataclass
class OptionQuote:
    contract: str          # OCC symbol
    underlying: str
    expiry: str            # YYYY-MM-DD
    strike: float
    right: str             # C | P
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    implied_vol: float | None = None

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2, 4)
        return self.last or self.ask or self.bid

    @property
    def spread_pct(self) -> float:
        m = self.mid
        if not m or self.ask <= 0 or self.bid <= 0:
            return 1.0
        return (self.ask - self.bid) / m


class QuoteProvider(Protocol):
    name: str

    def quote(self, symbol: str) -> Quote: ...
    def bars(self, symbol: str, lookback_days: int, interval: str) -> list[Bar]: ...


# ===========================================================================
# Equities via yfinance
# ===========================================================================
class YahooProvider:
    """
    Equity quotes, bars and option chains.

    Free and unreliable by nature - it is a scrape, not an API. It is fenced
    behind a breaker so an outage degrades the desk instead of hanging it.
    Swap in Polygon/Tradier/Alpaca here when there is budget for real data.
    """
    name = "yahoo"

    def __init__(self) -> None:
        self.breaker = get_breaker(
            "yahoo", settings.breaker_threshold, settings.breaker_cooldown_s
        )

    def _ticker(self, symbol: str):
        import yfinance as yf
        pace("yahoo", 2.0)
        return yf.Ticker(symbol, session=SESSION)

    def _guarded(self, label: str, fn, *a, **kw):
        self.breaker.guard()
        try:
            out = budget(label, settings.data_call_budget_s, fn, *a, **kw)
        except (CallTimeout, Exception) as exc:
            self.breaker.record_failure()
            raise DataUnavailable(f"{label}: {exc}") from exc
        self.breaker.record_success()
        return out

    def quote(self, symbol: str) -> Quote:
        def _fetch() -> Quote:
            t = self._ticker(symbol)
            hist = t.history(period="5d", interval="1d")
            if hist is None or hist.empty:
                raise DataUnavailable(f"no history for {symbol}")
            last = hist.iloc[-1]
            prev = hist.iloc[-2] if len(hist) > 1 else last
            return Quote(
                symbol=symbol,
                price=float(last["Close"]),
                ts=datetime.now(tz=timezone.utc),
                prev_close=float(prev["Close"]),
                day_high=float(last["High"]),
                day_low=float(last["Low"]),
                volume=float(last["Volume"]),
            )

        return self._guarded(f"yahoo.quote:{symbol}", _fetch)

    def bars(self, symbol: str, lookback_days: int = 30,
             interval: str = "1d") -> list[Bar]:
        def _fetch() -> list[Bar]:
            t = self._ticker(symbol)
            hist = t.history(period=f"{lookback_days}d", interval=interval)
            if hist is None or hist.empty:
                raise DataUnavailable(f"no bars for {symbol}")
            return [
                Bar(
                    ts=idx.to_pydatetime(),
                    open=float(r["Open"]), high=float(r["High"]),
                    low=float(r["Low"]), close=float(r["Close"]),
                    volume=float(r["Volume"]),
                )
                for idx, r in hist.iterrows()
            ]

        return self._guarded(f"yahoo.bars:{symbol}", _fetch)

    def option_chain(self, symbol: str, spot: float) -> list[OptionQuote]:
        """Near-the-money contracts inside the configured DTE window."""
        def _fetch() -> list[OptionQuote]:
            t = self._ticker(symbol)
            expiries = list(t.options or [])
            if not expiries:
                raise DataUnavailable(f"no expiries for {symbol}")

            today = datetime.now(tz=timezone.utc).date()
            usable = []
            for e in expiries:
                try:
                    dte = (datetime.strptime(e, "%Y-%m-%d").date() - today).days
                except ValueError:
                    continue
                if settings.target_dte_min <= dte <= settings.target_dte_max:
                    usable.append((dte, e))
            if not usable:
                raise DataUnavailable(f"no expiry in DTE window for {symbol}")

            usable.sort()
            _, expiry = usable[len(usable) // 2]
            chain = t.option_chain(expiry)

            out: list[OptionQuote] = []
            lo, hi = spot * 0.90, spot * 1.10
            for frame, right in ((chain.calls, "C"), (chain.puts, "P")):
                if frame is None or frame.empty:
                    continue
                near = frame[(frame["strike"] >= lo) & (frame["strike"] <= hi)]
                for _, row in near.iterrows():
                    out.append(OptionQuote(
                        contract=str(row.get("contractSymbol", "")),
                        underlying=symbol,
                        expiry=expiry,
                        strike=float(row["strike"]),
                        right=right,
                        bid=float(row.get("bid") or 0),
                        ask=float(row.get("ask") or 0),
                        last=float(row.get("lastPrice") or 0),
                        volume=int(row.get("volume") or 0),
                        open_interest=int(row.get("openInterest") or 0),
                        implied_vol=float(row["impliedVolatility"])
                        if row.get("impliedVolatility") is not None else None,
                    ))
            if not out:
                raise DataUnavailable(f"empty near-the-money chain for {symbol}")
            return out

        return self._guarded(f"yahoo.chain:{symbol}", _fetch)


# ===========================================================================
# Crypto: Coinbase primary, Kraken fallback
# ===========================================================================
_GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
# Coinbase /products/{id}/candles hard-caps at 300 aggregations per request.
# 14d of 1h bars is 336 → 400 "granularity too small for the requested time
# range". Page so each window stays under the cap.
_COINBASE_MAX_CANDLES = 300


class CoinbaseProvider:
    """Public market data. No API key required for candles or ticker."""
    name = "coinbase"
    BASE = "https://api.exchange.coinbase.com"

    def __init__(self) -> None:
        self.breaker = get_breaker(
            "coinbase", settings.breaker_threshold, settings.breaker_cooldown_s
        )

    def _get(self, path: str, params: dict | None = None) -> Any:
        self.breaker.guard()
        pace("coinbase", 0.4)
        try:
            r = budget(
                f"coinbase{path}", settings.data_call_budget_s,
                SESSION.get, f"{self.BASE}{path}", params=params or {},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            self.breaker.record_failure()
            raise DataUnavailable(f"coinbase {path}: {exc}") from exc
        self.breaker.record_success()
        return data

    def quote(self, symbol: str) -> Quote:
        product = symbol.upper()
        tick = self._get(f"/products/{product}/ticker")
        stats = self._get(f"/products/{product}/stats")
        price = float(tick["price"])
        return Quote(
            symbol=product,
            price=price,
            ts=datetime.now(tz=timezone.utc),
            prev_close=float(stats.get("open") or price),
            day_high=float(stats.get("high") or price),
            day_low=float(stats.get("low") or price),
            volume=float(stats.get("volume") or 0),
            meta={"venue": "coinbase"},
        )

    def bars(self, symbol: str, lookback_days: int = 7,
             interval: str = "1h") -> list[Bar]:
        gran = _GRANULARITY.get(interval, 3600)
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=lookback_days)
        # Leave one candle of headroom so ceiling math never hits 301.
        max_span = timedelta(seconds=(_COINBASE_MAX_CANDLES - 1) * gran)

        # Dedupe by candle open time while paging newest → oldest.
        by_ts: dict[datetime, Bar] = {}
        cursor_end = end
        product = symbol.upper()
        while cursor_end > start:
            cursor_start = max(start, cursor_end - max_span)
            raw = self._get(
                f"/products/{product}/candles",
                {
                    "granularity": gran,
                    "start": cursor_start.isoformat(),
                    "end": cursor_end.isoformat(),
                },
            )
            # Coinbase returns newest-first: [time, low, high, open, close, volume]
            if not raw:
                break
            for row in raw:
                ts = datetime.fromtimestamp(row[0], tz=timezone.utc)
                by_ts[ts] = Bar(
                    ts=ts,
                    low=float(row[1]), high=float(row[2]),
                    open=float(row[3]), close=float(row[4]),
                    volume=float(row[5]),
                )
            if cursor_start <= start:
                break
            # Step the window back; avoid re-fetching the boundary candle.
            cursor_end = cursor_start

        bars = sorted(by_ts.values(), key=lambda b: b.ts)
        if not bars:
            raise DataUnavailable(f"no candles for {symbol}")
        return bars


class KrakenProvider:
    """Fallback venue. Same shape, different symbols."""
    name = "kraken"
    BASE = "https://api.kraken.com/0/public"
    _MAP = {"BTC-USD": "XBTUSD", "ETH-USD": "ETHUSD", "SOL-USD": "SOLUSD"}
    _INTERVAL_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 1440}

    def __init__(self) -> None:
        self.breaker = get_breaker(
            "kraken", settings.breaker_threshold, settings.breaker_cooldown_s
        )

    def _pair(self, symbol: str) -> str:
        return self._MAP.get(symbol.upper(), symbol.upper().replace("-", ""))

    def _get(self, path: str, params: dict) -> Any:
        self.breaker.guard()
        pace("kraken", 1.0)
        try:
            r = budget(
                f"kraken{path}", settings.data_call_budget_s,
                SESSION.get, f"{self.BASE}{path}", params=params,
            )
            r.raise_for_status()
            body = r.json()
            if body.get("error"):
                raise DataUnavailable(str(body["error"]))
            data = body["result"]
        except Exception as exc:
            self.breaker.record_failure()
            raise DataUnavailable(f"kraken {path}: {exc}") from exc
        self.breaker.record_success()
        return data

    def quote(self, symbol: str) -> Quote:
        pair = self._pair(symbol)
        res = self._get("/Ticker", {"pair": pair})
        key = next(iter(res))
        t = res[key]
        price = float(t["c"][0])
        return Quote(
            symbol=symbol.upper(),
            price=price,
            ts=datetime.now(tz=timezone.utc),
            prev_close=float(t["o"]),
            day_high=float(t["h"][1]),
            day_low=float(t["l"][1]),
            volume=float(t["v"][1]),
            meta={"venue": "kraken"},
        )

    def bars(self, symbol: str, lookback_days: int = 7,
             interval: str = "1h") -> list[Bar]:
        mins = self._INTERVAL_MIN.get(interval, 60)
        since = int((datetime.now(tz=timezone.utc)
                     - timedelta(days=lookback_days)).timestamp())
        res = self._get("/OHLC", {"pair": self._pair(symbol),
                                  "interval": mins, "since": since})
        key = next(k for k in res if k != "last")
        bars = [
            Bar(
                ts=datetime.fromtimestamp(int(r[0]), tz=timezone.utc),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[6]),
            )
            for r in res[key]
        ]
        if not bars:
            raise DataUnavailable(f"no OHLC for {symbol}")
        return bars


class CryptoChain:
    """Try each venue in order. Only fails when all of them do."""
    name = "crypto-chain"

    def __init__(self, providers: list[Any] | None = None) -> None:
        self.providers = providers or [CoinbaseProvider(), KrakenProvider()]

    def _try(self, method: str, *a, **kw):
        errors = []
        for p in self.providers:
            try:
                return getattr(p, method)(*a, **kw)
            except (DataUnavailable, BreakerOpen) as exc:
                errors.append(f"{p.name}: {exc}")
                log.warning("[DATA FAILOVER] %s.%s -> %s", p.name, method, exc)
        raise DataUnavailable(f"all crypto venues failed | {' | '.join(errors)}")

    def quote(self, symbol: str) -> Quote:
        return self._try("quote", symbol)

    def bars(self, symbol: str, lookback_days: int = 7,
             interval: str = "1h") -> list[Bar]:
        return self._try("bars", symbol, lookback_days, interval)


# ------------------------------------------------------------------ registry
_equity: Any | None = None
_crypto: CryptoChain | None = None


def equity_provider() -> Any:
    """
    Equity provider selected by MARKET_DATA_PROVIDER.

    yahoo  — per-symbol scrape (legacy, high request footprint)
    alpaca — batched quotes/bars/options, rate-governed
    """
    global _equity
    if _equity is None:
        if settings.market_data_provider == "alpaca":
            from app.data.alpaca import AlpacaProvider
            _equity = AlpacaProvider()
        else:
            _equity = YahooProvider()
    return _equity


def crypto_provider() -> CryptoChain:
    global _crypto
    if _crypto is None:
        _crypto = CryptoChain()
    return _crypto


def reset_providers() -> None:
    """Drop cached providers. Used by tests and after config changes."""
    global _equity, _crypto
    _equity = None
    _crypto = None
