"""
Alpaca market data. Batched equities + options. Every call is budgeted.

Free Basic: IEX equities (~2-3% of US volume, not SIP) and indicative options
rather than OPRA. Adequate for paper, not for money.

Request footprint (measured design target):
  - quotes_many / bars_many: 1 request for the whole universe
  - option_chain: 1 request per symbol that survives the exact bound gate
  - mark_many: 1 request for all open instruments
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlencode

import requests

from app.config import settings
from app.data.providers import Bar, DataUnavailable, OptionQuote, Quote
from app.resilience.governor import RateLimitExceeded, alpaca_governor
from app.resilience.timeouts import CallTimeout, budget

log = logging.getLogger("data.alpaca")

DATA_URL = "https://data.alpaca.markets"
# Options snapshot lives on the same host under /v1beta1 for free tier.


class AlpacaProvider:
    name = "alpaca"

    def __init__(
        self,
        *,
        key_id: str | None = None,
        secret: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.key_id = key_id if key_id is not None else settings.alpaca_key_id
        self.secret = secret if secret is not None else settings.alpaca_secret_key
        self.session = session or requests.Session()
        self._req_count = 0

    # --------------------------------------------------------------- HTTP
    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret,
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> Any:
        gov = alpaca_governor()
        gov.acquire(block=True, timeout=settings.data_call_budget_s)
        url = f"{DATA_URL}{path}"
        if params:
            # Alpaca accepts repeated / comma-separated symbols.
            url = f"{url}?{urlencode(params, doseq=True)}"

        def _do() -> Any:
            r = self.session.get(
                url,
                headers=self._headers(),
                timeout=settings.http_timeout_s,
            )
            if r.status_code == 429:
                gov.record_429()
                raise DataUnavailable("alpaca 429 rate limited")
            if r.status_code >= 400:
                raise DataUnavailable(
                    f"alpaca {path} HTTP {r.status_code}: {r.text[:200]}"
                )
            return r.json()

        try:
            out = budget(f"alpaca{path}", settings.data_call_budget_s, _do)
        except (CallTimeout, RateLimitExceeded) as exc:
            raise DataUnavailable(str(exc)) from exc
        except DataUnavailable:
            raise
        except Exception as exc:
            raise DataUnavailable(f"alpaca {path}: {exc}") from exc
        self._req_count += 1
        return out

    @property
    def request_count(self) -> int:
        return self._req_count

    def reset_request_count(self) -> None:
        self._req_count = 0

    # --------------------------------------------------------------- quotes
    def quote(self, symbol: str) -> Quote:
        out = self.quotes_many([symbol])
        if symbol.upper() not in out:
            raise DataUnavailable(f"no alpaca quote for {symbol}")
        return out[symbol.upper()]

    def quotes_many(self, symbols: Iterable[str]) -> dict[str, Quote]:
        syms = sorted({s.upper() for s in symbols if s})
        if not syms:
            return {}
        raw = self._get(
            "/v2/stocks/quotes/latest",
            {"symbols": ",".join(syms), "feed": settings.alpaca_feed},
        )
        quotes = raw.get("quotes") or {}
        # Prefer a two-sided quote. After hours IEX often leaves one side at 0
        # (or a penny bid) while the last trade is still usable — without a
        # trade fallback, option strike windows collapse around $0.01.
        def _two_sided(q: dict) -> bool:
            return float(q.get("bp") or 0) > 0 and float(q.get("ap") or 0) > 0

        need_trade = [s for s in syms if not _two_sided(quotes.get(s) or {})]
        trades: dict = {}
        if need_trade:
            try:
                t_raw = self._get(
                    "/v2/stocks/trades/latest",
                    {"symbols": ",".join(need_trade), "feed": settings.alpaca_feed},
                )
                trades = t_raw.get("trades") or {}
            except DataUnavailable:
                trades = {}

        out: dict[str, Quote] = {}
        now = datetime.now(tz=timezone.utc)
        for sym in syms:
            q = quotes.get(sym) or {}
            t = trades.get(sym) or {}
            bid = float(q.get("bp") or 0)
            ask = float(q.get("ap") or 0)
            last = float(t.get("p") or 0)
            mid = 0.0
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
            elif last > 0:
                mid = last
            elif ask > 0:
                mid = ask
            elif bid > 0:
                mid = bid
            if mid <= 0:
                continue
            out[sym] = Quote(
                symbol=sym,
                price=mid,
                ts=now,
                prev_close=None,
                day_high=None,
                day_low=None,
                volume=None,
                meta={"venue": "alpaca", "feed": settings.alpaca_feed,
                      "bid": bid, "ask": ask, "last": last},
            )
        return out

    # ---------------------------------------------------------------- bars
    def bars(
        self, symbol: str, lookback_days: int = 30, interval: str = "1d"
    ) -> list[Bar]:
        many = self.bars_many([symbol], lookback_days=lookback_days, interval=interval)
        bars = many.get(symbol.upper(), [])
        if not bars:
            raise DataUnavailable(f"no alpaca bars for {symbol}")
        return bars

    def bars_many(
        self,
        symbols: Iterable[str],
        lookback_days: int = 30,
        interval: str = "1d",
    ) -> dict[str, list[Bar]]:
        syms = sorted({s.upper() for s in symbols if s})
        if not syms:
            return {}
        tf = _timeframe(interval)
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=max(lookback_days, 1) + 2)
        raw = self._get(
            "/v2/stocks/bars",
            {
                "symbols": ",".join(syms),
                "timeframe": tf,
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 10000,
                "adjustment": "raw",
                "feed": settings.alpaca_feed,
            },
        )
        bars_map = raw.get("bars") or {}
        out: dict[str, list[Bar]] = {}
        for sym in syms:
            rows = bars_map.get(sym) or []
            series: list[Bar] = []
            for r in rows:
                try:
                    ts = datetime.fromisoformat(
                        str(r["t"]).replace("Z", "+00:00")
                    )
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    series.append(Bar(
                        ts=ts,
                        open=float(r["o"]),
                        high=float(r["h"]),
                        low=float(r["l"]),
                        close=float(r["c"]),
                        volume=float(r.get("v") or 0),
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
            series.sort(key=lambda b: b.ts)
            if series:
                # prev_close enrichment for quotes happens elsewhere via bars.
                out[sym] = series
        return out

    # ------------------------------------------------------------- options
    def option_chain(self, symbol: str, spot: float) -> list[OptionQuote]:
        """
        Near-the-money option snapshots within the configured DTE window.

        Free tier is the *indicative* options feed (OPRA needs a signed
        agreement and 403s). Request strike + expiry windows server-side:
        without them Alpaca returns an arbitrary page of contracts (often
        only 0-DTE), and client-side DTE filters empty the chain.
        """
        today = datetime.now(tz=timezone.utc).date()
        # ±15% absorbs after-hours quote/trade drift while staying NTM.
        # (Previously ±10% client-only, with no server filters.)
        lo, hi = spot * 0.85, spot * 1.15
        exp_gte = (today + timedelta(days=settings.target_dte_min)).isoformat()
        exp_lte = (today + timedelta(days=settings.target_dte_max)).isoformat()
        feed = settings.alpaca_options_feed
        params = {
            "feed": feed,
            "limit": 250,
            "strike_price_gte": f"{lo:.2f}",
            "strike_price_lte": f"{hi:.2f}",
            "expiration_date_gte": exp_gte,
            "expiration_date_lte": exp_lte,
        }
        log.debug(
            "alpaca option_chain %s spot=%.4f feed=%s strike=[%s,%s] exp=[%s,%s]",
            symbol.upper(), spot, feed,
            params["strike_price_gte"], params["strike_price_lte"],
            exp_gte, exp_lte,
        )
        raw = self._get(
            f"/v1beta1/options/snapshots/{symbol.upper()}",
            params,
        )

        snapshots = raw.get("snapshots") or {}
        if not snapshots and isinstance(raw.get(symbol.upper()), dict):
            snapshots = raw[symbol.upper()]

        out: list[OptionQuote] = []
        items = snapshots.items() if isinstance(snapshots, dict) else []
        for contract, snap in items:
            try:
                parsed = _parse_occ(str(contract))
                if parsed is None:
                    continue
                und, expiry, right, strike = parsed
                dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days
                if not (settings.target_dte_min <= dte <= settings.target_dte_max):
                    continue
                if not (lo <= strike <= hi):
                    continue
                quote = (snap or {}).get("latestQuote") or {}
                trade = (snap or {}).get("latestTrade") or {}
                greeks = (snap or {}).get("greeks") or {}
                bid = float(quote.get("bp") or 0)
                ask = float(quote.get("ap") or 0)
                last = float(trade.get("p") or 0)
                out.append(OptionQuote(
                    contract=str(contract),
                    underlying=und,
                    expiry=expiry,
                    strike=strike,
                    right=right,
                    bid=bid,
                    ask=ask,
                    last=last,
                    volume=int((snap or {}).get("dailyBar", {}).get("v") or 0),
                    open_interest=int((snap or {}).get("openInterest") or 0),
                    implied_vol=(
                        float(greeks["iv"]) if greeks.get("iv") is not None else None
                    ),
                ))
            except (TypeError, ValueError, KeyError):
                continue

        if not out:
            raise DataUnavailable(f"empty alpaca chain for {symbol}")
        return out

    # --------------------------------------------------------------- marks
    def mark_options(self, contracts: list[str]) -> dict[str, float]:
        """Latest mid for a set of OCC symbols. One request."""
        if not contracts:
            return {}
        # Snapshots for specific contracts.
        raw = self._get(
            "/v1beta1/options/snapshots",
            {"symbols": ",".join(contracts), "feed": settings.alpaca_options_feed},
        )
        snaps = raw.get("snapshots") or {}
        out: dict[str, float] = {}
        for c in contracts:
            snap = snaps.get(c) or {}
            q = snap.get("latestQuote") or {}
            t = snap.get("latestTrade") or {}
            bid = float(q.get("bp") or 0)
            ask = float(q.get("ap") or 0)
            last = float(t.get("p") or 0)
            if bid > 0 and ask > 0:
                out[c] = round((bid + ask) / 2, 4)
            elif last > 0:
                out[c] = last
            elif ask > 0:
                out[c] = ask
            elif bid > 0:
                out[c] = bid
        return out

    def mark_equities(self, symbols: list[str]) -> dict[str, float]:
        quotes = self.quotes_many(symbols)
        return {s: q.price for s, q in quotes.items()}


def _timeframe(interval: str) -> str:
    return {
        "1m": "1Min", "5m": "5Min", "15m": "15Min",
        "1h": "1Hour", "1d": "1Day",
    }.get(interval, "1Day")


def _parse_occ(symbol: str) -> tuple[str, str, str, float] | None:
    """
    OCC: ROOT + YYMMDD + C/P + strike*1000 zero-padded 8.
    e.g. AAPL240119C00150000
    """
    s = symbol.strip().upper()
    if len(s) < 15:
        return None
    # Root is variable length; right is C/P before 8-digit strike.
    try:
        right_idx = max(s.rfind("C"), s.rfind("P"))
        if right_idx < 6:
            return None
        right = s[right_idx]
        if right not in {"C", "P"}:
            return None
        strike_raw = s[right_idx + 1:]
        if len(strike_raw) != 8 or not strike_raw.isdigit():
            return None
        date_part = s[right_idx - 6: right_idx]
        if not date_part.isdigit():
            return None
        root = s[: right_idx - 6]
        yy, mm, dd = int(date_part[:2]), int(date_part[2:4]), int(date_part[4:6])
        year = 2000 + yy
        expiry = f"{year:04d}-{mm:02d}-{dd:02d}"
        strike = int(strike_raw) / 1000.0
        return root, expiry, right, strike
    except (ValueError, IndexError):
        return None
