"""
Historical bar cache for backtests.

Caches CSV under data/cache/ so re-runs do not hit the network.
Does not silently pad gaps — reports them.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.data.providers import Bar

log = logging.getLogger("backtest.data")

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "cache"


@dataclass
class SeriesReport:
    symbol: str
    interval: str
    n_bars: int
    first: datetime | None
    last: datetime | None
    gaps: int
    source: str
    path: Path


def _parse_ts(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _write_csv(path: Path, bars: list[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([
                b.ts.astimezone(timezone.utc).isoformat(timespec="seconds"),
                f"{b.open:.8f}", f"{b.high:.8f}", f"{b.low:.8f}",
                f"{b.close:.8f}", f"{b.volume:.4f}",
            ])


def _read_csv(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open() as f:
        r = csv.DictReader(f)
        for row in r:
            bars.append(Bar(
                ts=_parse_ts(row["ts"]),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    return bars


def count_gaps(bars: list[Bar], interval: str) -> int:
    """Count missing steps larger than 1.5× expected spacing."""
    if len(bars) < 2:
        return 0
    if interval == "1d":
        expected = timedelta(days=1)
    elif interval == "1h":
        expected = timedelta(hours=1)
    else:
        expected = bars[1].ts - bars[0].ts
    gaps = 0
    for a, b in zip(bars, bars[1:]):
        if b.ts - a.ts > expected * 1.5:
            # week-ends on daily are normal; only count multi-day gaps > 4d
            if interval == "1d" and (b.ts - a.ts) <= timedelta(days=4):
                continue
            gaps += 1
    return gaps


def load_equity_daily(symbol: str, *, years: float = 2.0,
                      force: bool = False) -> tuple[list[Bar], SeriesReport]:
    """
    Daily equity bars. Prefer Alpaca (authenticated, reliable); fall back to
    yfinance. Reports actual range — free Alpaca often returns ~16–24 months,
    not a full 5y.
    """
    path = CACHE_DIR / f"equity_1d_{symbol.upper()}.csv"
    if path.exists() and not force:
        bars = _read_csv(path)
        return bars, SeriesReport(
            symbol=symbol.upper(), interval="1d", n_bars=len(bars),
            first=bars[0].ts if bars else None,
            last=bars[-1].ts if bars else None,
            gaps=count_gaps(bars, "1d"), source="cache", path=path,
        )

    bars: list[Bar] = []
    source = ""
    lookback = int(years * 365) + 30
    # 1) Alpaca
    try:
        from app.data.providers import equity_provider
        data = equity_provider()
        bars = data.bars(symbol, lookback_days=lookback, interval="1d")
        source = getattr(data, "name", "equity_provider")
    except Exception as exc:
        log.warning("alpaca/equity bars %s failed: %s", symbol, exc)
        bars = []

    # 2) yfinance fallback
    if not bars:
        try:
            import yfinance as yf
            period = "2y" if years <= 2.1 else "5y"
            t = yf.Ticker(symbol)
            hist = t.history(period=period, interval="1d", auto_adjust=True)
            if hist is not None and not hist.empty:
                for idx, row in hist.iterrows():
                    ts = idx.to_pydatetime()
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    else:
                        ts = ts.astimezone(timezone.utc)
                    bars.append(Bar(
                        ts=ts,
                        open=float(row["Open"]), high=float(row["High"]),
                        low=float(row["Low"]), close=float(row["Close"]),
                        volume=float(row["Volume"]),
                    ))
                source = "yfinance"
        except Exception as exc:
            log.warning("yfinance bars %s failed: %s", symbol, exc)

    if not bars:
        raise RuntimeError(f"no daily history for {symbol}")

    _write_csv(path, bars)
    return bars, SeriesReport(
        symbol=symbol.upper(), interval="1d", n_bars=len(bars),
        first=bars[0].ts if bars else None,
        last=bars[-1].ts if bars else None,
        gaps=count_gaps(bars, "1d"), source=source, path=path,
    )


def load_crypto_hourly(symbol: str = "BTC-USD", *, days: int = 365,
                       force: bool = False) -> tuple[list[Bar], SeriesReport]:
    """Hourly crypto bars via Coinbase public candles, paged."""
    path = CACHE_DIR / f"crypto_1h_{symbol.upper().replace('-', '')}.csv"
    if path.exists() and not force:
        bars = _read_csv(path)
        return bars, SeriesReport(
            symbol=symbol.upper(), interval="1h", n_bars=len(bars),
            first=bars[0].ts if bars else None,
            last=bars[-1].ts if bars else None,
            gaps=count_gaps(bars, "1h"), source="cache", path=path,
        )

    import requests

    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=days)
    gran = 3600
    max_span = timedelta(seconds=299 * gran)
    by_ts: dict[int, Bar] = {}
    cursor = end
    product = symbol.upper()
    nreq = 0
    while cursor > start and nreq < 80:
        cs = max(start, cursor - max_span)
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={
                "granularity": gran,
                "start": cs.isoformat(),
                "end": cursor.isoformat(),
            },
            timeout=30,
            headers={"User-Agent": "janus-desk-backtest"},
        )
        nreq += 1
        if r.status_code != 200:
            log.warning("coinbase %s HTTP %s", product, r.status_code)
            break
        raw = r.json()
        if not raw:
            break
        for row in raw:
            t = int(row[0])
            by_ts[t] = Bar(
                ts=datetime.fromtimestamp(t, tz=timezone.utc),
                low=float(row[1]), high=float(row[2]),
                open=float(row[3]), close=float(row[4]),
                volume=float(row[5]),
            )
        oldest = min(int(row[0]) for row in raw)
        cursor = datetime.fromtimestamp(oldest, tz=timezone.utc) - timedelta(seconds=1)

    bars = [by_ts[k] for k in sorted(by_ts)]
    if not bars:
        raise RuntimeError(f"no hourly history for {symbol}")
    _write_csv(path, bars)
    return bars, SeriesReport(
        symbol=symbol.upper(), interval="1h", n_bars=len(bars),
        first=bars[0].ts, last=bars[-1].ts,
        gaps=count_gaps(bars, "1h"), source=f"coinbase({nreq} reqs)", path=path,
    )


def align_calendar(series: dict[str, list[Bar]]) -> list[datetime]:
    """Union of all daily timestamps sorted (equity calendar)."""
    ts: set[datetime] = set()
    for bars in series.values():
        for b in bars:
            ts.add(b.ts)
    return sorted(ts)


def bars_upto(bars: list[Bar], t: datetime) -> list[Bar]:
    """Strict no-lookahead slice: only bars with ts <= t."""
    return [b for b in bars if b.ts <= t]


def bar_on_or_before(bars: list[Bar], t: datetime) -> Bar | None:
    upto = bars_upto(bars, t)
    return upto[-1] if upto else None
