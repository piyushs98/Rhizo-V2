"""
Deterministic scoring. The only thing in this system with authority to say
EXECUTE.

THE RULE, and it is absolute: no string produced by a language model enters
this module. Not as a feature, not as a keyword match, not as a sentiment
label. Post-mortem #4 in v1 was a substring search for the word "warning"
inside a manager's prose, which silently capped every score at 50-65 and
suppressed executions for an unknown length of time. Every function below
takes floats and returns floats.

Three pillars, each scored 0-100, then combined by configurable weights that
must sum to 100.

  liquidity  can I get in and out at a fair price
  technical  is the price structure favourable right now
  sentiment  is the broader tape leaning my way
"""
from __future__ import annotations

from app.config import settings
from app.data.providers import Bar, OptionQuote
from app.domain.models import ScoreCard
from app.engine import indicators as ind

DEFAULT_WEIGHTS = {"liquidity": 30.0, "technical": 40.0, "sentiment": 30.0}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _grade(value: float, best: float, worst: float) -> float:
    """
    Map a value onto 0-100 by linear interpolation between `worst` and `best`.
    Handles either direction. Graded, not a cliff - a near-miss should score
    near-miss, not zero.
    """
    if best == worst:
        return 50.0
    return _clamp((value - worst) / (best - worst) * 100.0)


# ===========================================================================
# Pillar 1 - liquidity
# ===========================================================================
def score_option_liquidity(q: OptionQuote) -> tuple[float, dict[str, float]]:
    """Spread dominates: it is the cost you pay twice."""
    spread = q.spread_pct
    s_spread = _grade(spread, best=0.01, worst=settings.max_spread_pct)
    s_volume = _grade(float(q.volume), best=2000.0, worst=0.0)
    # depth prefers OI; falls back to volume when the feed omits OI
    # (Alpaca indicative always returns openInterest=0 / absent).
    depth = float(q.depth)
    s_oi = _grade(depth, best=5000.0, worst=float(settings.min_open_interest))

    total = s_spread * 0.60 + s_volume * 0.25 + s_oi * 0.15
    return round(total, 2), {
        "spread_pct": round(spread, 4),
        "volume": float(q.volume),
        "open_interest": float(q.open_interest),
        "depth": depth,
        "s_spread": round(s_spread, 1),
        "s_volume": round(s_volume, 1),
        "s_oi": round(s_oi, 1),
    }


def score_spot_liquidity(
    bars: list[Bar], spread_pct: float | None = None
) -> tuple[float, dict[str, float]]:
    """
    Spot / share liquidity.

    - Spread: real bid/ask fraction when provided. If missing, estimate from
      the last bar's range (never a constant 85).
    - Volume: same volume-ratio grade as before.
    - Turnover: last bar volume vs the symbol's own trailing median volume
      (not an absolute 5000 floor that saturates every mega-cap at 100).
    """
    inputs: dict[str, float] = {}

    vr = ind.volume_ratio(bars, 20) or 1.0
    inputs["volume_ratio"] = round(vr, 3)
    # Healthy band is 0.7-2.5x. Dead tape and blow-off spikes both score down.
    s_vol = _grade(min(vr, 2.5), best=1.6, worst=0.3) if vr <= 2.5 else 60.0

    spread_source = 1.0  # 1 = quoted, 0 = proxy
    if spread_pct is None and bars:
        b = bars[-1]
        if b.close > 0 and b.high >= b.low:
            # Daily range is wider than the spread; scale to a spread-like size.
            spread_pct = max(1e-5, min(0.02, (b.high - b.low) / b.close * 0.05))
            spread_source = 0.0
    if spread_pct is None:
        s_spread = 50.0
        inputs["spread_pct"] = 0.0
    else:
        # Equities: best ~2 bps, bad ~1%. Crypto quotes can be tighter.
        s_spread = _grade(spread_pct, best=0.0002, worst=0.01)
        inputs["spread_pct"] = round(spread_pct, 6)
    inputs["spread_source"] = spread_source

    # Relative turnover: latest volume / trailing median volume.
    look = min(20, len(bars))
    vols = [float(b.volume) for b in bars[-look:] if b.volume and b.volume > 0]
    if len(vols) >= 5:
        from statistics import median
        med = median(vols[:-1]) if len(vols) > 1 else median(vols)
        last_v = vols[-1]
        if med > 0:
            turn_ratio = last_v / med
        else:
            turn_ratio = 1.0
        inputs["turnover_ratio"] = round(turn_ratio, 3)
        inputs["turnover_median"] = round(med, 2)
        s_turn = _grade(min(turn_ratio, 3.0), best=1.5, worst=0.4)
    else:
        inputs["turnover_ratio"] = 1.0
        s_turn = 50.0

    total = s_spread * 0.50 + s_vol * 0.30 + s_turn * 0.20
    inputs.update({"s_spread": round(s_spread, 1), "s_volume": round(s_vol, 1),
                   "s_turnover": round(s_turn, 1)})
    return round(total, 2), inputs


# ===========================================================================
# Pillar 2 - technical
# ===========================================================================
def score_technical(bars: list[Bar], *, bullish: bool = True
                    ) -> tuple[float, dict[str, float]]:
    """
    Four graded components. `bullish=False` mirrors every directional read,
    so a put setup is scored on its own merits rather than as 100-minus.
    """
    closes = [b.close for b in bars]
    inputs: dict[str, float] = {}

    # 1. Trend alignment (40%)
    t = ind.trend_score(closes)
    if not bullish:
        t = -t
    inputs["trend"] = round(t, 3)
    s_trend = _grade(t, best=0.6, worst=-0.4)

    # 2. RSI. Reward strength, penalise exhaustion at both ends. (25%)
    r = ind.rsi(closes, 14)
    if r is None:
        s_rsi, r = 50.0, 50.0
    else:
        eff = r if bullish else 100 - r
        # peak around 60: trending but not yet extended
        s_rsi = _clamp(100 - abs(eff - 60) * 2.2)
    inputs["rsi"] = round(r, 1)

    # 3. Position in the recent range (20%)
    rp = ind.range_position(bars, 20)
    if rp is None:
        s_range = 50.0
    else:
        eff_rp = rp if bullish else 1 - rp
        s_range = _grade(eff_rp, best=0.80, worst=0.15)
        inputs["range_position"] = round(rp, 3)

    # 4. Volatility. Some is required; too much is unpriceable. (15%)
    ap = ind.atr_pct(bars, 14)
    if ap is None:
        s_vol = 50.0
    else:
        inputs["atr_pct"] = round(ap, 4)
        s_vol = _clamp(100 - abs(ap - 0.022) * 2200)

    total = s_trend * 0.40 + s_rsi * 0.25 + s_range * 0.20 + s_vol * 0.15
    inputs.update({
        "s_trend": round(s_trend, 1), "s_rsi": round(s_rsi, 1),
        "s_range": round(s_range, 1), "s_vol": round(s_vol, 1),
    })
    return round(total, 2), inputs


# ===========================================================================
# Pillar 3 - sentiment
# ===========================================================================
def score_sentiment(
    *,
    change_pct: float,
    benchmark_change_pct: float | None = None,
    momentum_pct: float | None = None,
    volume_ratio: float | None = None,
    bullish: bool = True,
    news_bias: float = 0.0,
) -> tuple[float, dict[str, float]]:
    """
    Tape-derived base score, plus an optional numeric news tilt.

    `news_bias` is a float in [-1, 1] (already validated by the news agent).
    This module never imports the agent or the LLM chain. The tilt is capped
    at NEWS_BIAS_WEIGHT of the pillar's headroom toward 0 or 100, and the
    result is clamped twice: once on the bias, once on the final score.
    """
    sign = 1.0 if bullish else -1.0
    inputs: dict[str, float] = {"change_pct": round(change_pct, 3)}

    # Relative strength (40%)
    if benchmark_change_pct is None:
        s_rel = 50.0
    else:
        rel = (change_pct - benchmark_change_pct) * sign
        inputs["relative_strength"] = round(rel, 3)
        s_rel = _grade(rel, best=1.5, worst=-1.5)

    # Momentum (35%)
    if momentum_pct is None:
        s_mom = 50.0
    else:
        m = momentum_pct * sign
        inputs["momentum_pct"] = round(momentum_pct, 3)
        s_mom = _grade(m, best=8.0, worst=-6.0)

    # Volume confirmation (25%)
    if volume_ratio is None:
        s_conf = 50.0
    else:
        inputs["volume_ratio"] = round(volume_ratio, 3)
        s_conf = _grade(min(volume_ratio, 3.0), best=1.8, worst=0.4)

    total = s_rel * 0.40 + s_mom * 0.35 + s_conf * 0.25

    # Bounded news tilt. Bias is clamped first; score is clamped after.
    bias = _clamp(float(news_bias), -1.0, 1.0)
    weight = float(settings.news_bias_weight)
    if bias != 0.0 and weight > 0:
        if bias > 0:
            headroom = 100.0 - total
            total = total + headroom * weight * bias
        else:
            headroom = total
            total = total + headroom * weight * bias  # bias negative
    inputs["news_bias"] = round(bias, 4)
    inputs["news_tilt"] = round(total - (s_rel * 0.40 + s_mom * 0.35 + s_conf * 0.25), 3)

    total = _clamp(total)
    inputs.update({"s_relative": round(s_rel, 1), "s_momentum": round(s_mom, 1),
                   "s_confirmation": round(s_conf, 1)})
    return round(total, 2), inputs


# ===========================================================================
# Composition
# ===========================================================================
def load_weights() -> dict[str, float]:
    """Weights are tunable at runtime and must sum to 100."""
    from app.db import repositories as repo
    import json

    raw = repo.kv.get("scoring_weights", "")
    if not raw:
        return dict(DEFAULT_WEIGHTS)
    try:
        w = json.loads(raw)
        if abs(sum(w.values()) - 100.0) > 0.01:
            return dict(DEFAULT_WEIGHTS)
        return {k: float(w[k]) for k in DEFAULT_WEIGHTS}
    except Exception:
        return dict(DEFAULT_WEIGHTS)


def save_weights(w: dict[str, float]) -> None:
    from app.db import repositories as repo
    import json

    if set(w) != set(DEFAULT_WEIGHTS):
        raise ValueError(f"weights must have exactly these keys: {list(DEFAULT_WEIGHTS)}")
    if abs(sum(w.values()) - 100.0) > 0.01:
        raise ValueError(f"weights must sum to 100, got {sum(w.values())}")
    repo.kv.set("scoring_weights", json.dumps(w))


def compose(
    symbol: str,
    liquidity: tuple[float, dict],
    technical: tuple[float, dict],
    sentiment: tuple[float, dict],
    weights: dict[str, float] | None = None,
) -> ScoreCard:
    w = weights or load_weights()
    inputs: dict[str, float] = {}
    for prefix, (_, detail) in (
        ("liq", liquidity), ("tech", technical), ("sent", sentiment)
    ):
        inputs.update({f"{prefix}.{k}": v for k, v in detail.items()})

    return ScoreCard(
        symbol=symbol,
        liquidity=liquidity[0],
        technical=technical[0],
        sentiment=sentiment[0],
        weights=w,
        inputs=inputs,
    )


def meets_threshold(card: ScoreCard, threshold: float | None = None) -> bool:
    return card.total >= (threshold if threshold is not None
                          else settings.execute_threshold)
