"""
Scoring. The module that silently broke in v1 and stayed broken.

The regression guard at the bottom is the one that matters: it proves prose
cannot influence a score, because the functions do not accept prose.
"""
import inspect

import pytest

from app.data.providers import OptionQuote
from app.engine import scoring


def opt(bid, ask, volume=1000, oi=3000, strike=100.0):
    return OptionQuote(
        contract="TEST260130C00100000", underlying="TEST", expiry="2026-01-30",
        strike=strike, right="C", bid=bid, ask=ask, last=(bid + ask) / 2,
        volume=volume, open_interest=oi,
    )


# ------------------------------------------------------------- liquidity
def test_tight_spread_beats_wide_spread():
    tight, _ = scoring.score_option_liquidity(opt(2.00, 2.02))
    wide, _ = scoring.score_option_liquidity(opt(2.00, 2.60))
    assert tight > wide
    assert tight > 70


def test_illiquid_contract_scores_low():
    score, detail = scoring.score_option_liquidity(opt(1.00, 1.40, volume=2, oi=10))
    assert score < 40
    assert detail["s_volume"] < 10


def test_missing_oi_falls_back_to_volume_for_depth():
    """
    Alpaca indicative snapshots omit openInterest. Depth must still score
    from session volume so the floor does not zero every contract forever.
    """
    missing_oi = opt(2.00, 2.05, volume=800, oi=0)
    with_oi = opt(2.00, 2.05, volume=50, oi=800)
    assert missing_oi.depth == 800
    assert with_oi.depth == 800
    s_missing, d_missing = scoring.score_option_liquidity(missing_oi)
    s_oi, d_oi = scoring.score_option_liquidity(with_oi)
    assert d_missing["depth"] == 800
    assert s_missing > 50
    # Same depth units → comparable depth sub-score
    assert d_missing["s_oi"] == d_oi["s_oi"]


def test_liquidity_is_bounded():
    for q in (opt(0.01, 9.99, 0, 0), opt(5.00, 5.01, 99999, 99999)):
        s, _ = scoring.score_option_liquidity(q)
        assert 0 <= s <= 100


def test_spot_liquidity_spread_is_not_hardcoded_85(bars):
    """s_spread must vary with real/proxy spread — never a constant 85."""
    tight, d_tight = scoring.score_spot_liquidity(bars, spread_pct=0.0003)
    wide, d_wide = scoring.score_spot_liquidity(bars, spread_pct=0.008)
    assert d_tight["s_spread"] > d_wide["s_spread"]
    assert d_tight["s_spread"] != 85.0 or d_wide["s_spread"] != 85.0
    # Without explicit spread, proxy from bar range still grades (not constant).
    _, d_proxy = scoring.score_spot_liquidity(bars, spread_pct=None)
    assert "s_spread" in d_proxy
    assert d_proxy.get("spread_source") in (0.0, 1.0)


def test_spot_liquidity_turnover_is_relative(bars):
    """Turnover uses trailing median ratio, not absolute 5000 floor."""
    score, detail = scoring.score_spot_liquidity(bars, spread_pct=0.001)
    assert "turnover_ratio" in detail
    assert 0 <= score <= 100
    # Inflate last bar volume → higher turnover score
    from copy import deepcopy
    from dataclasses import replace
    hot = list(bars)
    last = hot[-1]
    hot[-1] = replace(last, volume=last.volume * 5)
    _, d_hot = scoring.score_spot_liquidity(hot, spread_pct=0.001)
    _, d_base = scoring.score_spot_liquidity(bars, spread_pct=0.001)
    assert d_hot["turnover_ratio"] > d_base["turnover_ratio"]


# -------------------------------------------------------------- technical
def test_uptrend_scores_higher_for_calls_than_puts(bars):
    bull, _ = scoring.score_technical(bars, bullish=True)
    bear, _ = scoring.score_technical(bars, bullish=False)
    assert bull > bear


def test_technical_needs_no_text(bars):
    """Signature check: floats in, floats out."""
    score, detail = scoring.score_technical(bars)
    assert isinstance(score, float)
    assert all(isinstance(v, (int, float)) for v in detail.values())


def test_flat_tape_scores_near_the_middle():
    from datetime import datetime, timedelta, timezone
    from app.data.providers import Bar

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    flat = [Bar(ts=base + timedelta(days=i), open=100, high=100.5,
                low=99.5, close=100, volume=1_000_000) for i in range(40)]
    score, _ = scoring.score_technical(flat)
    assert 25 < score < 75


# -------------------------------------------------------------- sentiment
def test_relative_strength_moves_the_score():
    strong, _ = scoring.score_sentiment(change_pct=2.5, benchmark_change_pct=0.2)
    weak, _ = scoring.score_sentiment(change_pct=-1.8, benchmark_change_pct=0.2)
    assert strong > weak


def test_sentiment_is_symmetric_for_puts():
    up, _ = scoring.score_sentiment(change_pct=-2.0, benchmark_change_pct=0.0,
                                    bullish=False)
    down, _ = scoring.score_sentiment(change_pct=2.0, benchmark_change_pct=0.0,
                                      bullish=False)
    assert up > down


def test_missing_inputs_are_neutral_not_zero():
    score, _ = scoring.score_sentiment(change_pct=0.0)
    assert 40 < score < 60


# ------------------------------------------------------------ composition
def test_total_respects_the_weights():
    card = scoring.compose("X", (100.0, {}), (0.0, {}), (0.0, {}),
                           weights={"liquidity": 30, "technical": 40, "sentiment": 30})
    assert card.total == pytest.approx(30.0)

    card = scoring.compose("X", (80.0, {}), (80.0, {}), (80.0, {}))
    assert card.total == pytest.approx(80.0)


def test_threshold():
    high = scoring.compose("X", (90.0, {}), (90.0, {}), (90.0, {}))
    low = scoring.compose("X", (40.0, {}), (40.0, {}), (40.0, {}))
    assert scoring.meets_threshold(high, 70)
    assert not scoring.meets_threshold(low, 70)


def test_weights_must_sum_to_100():
    with pytest.raises(ValueError):
        scoring.save_weights({"liquidity": 50, "technical": 40, "sentiment": 30})


def test_weights_round_trip():
    scoring.save_weights({"liquidity": 20, "technical": 50, "sentiment": 30})
    assert scoring.load_weights() == {"liquidity": 20.0, "technical": 50.0,
                                      "sentiment": 30.0}


# ================= the v1 regression guard ==============================
def test_no_scoring_function_accepts_text():
    """
    Post-mortem #4: a substring match for "warning" inside LLM prose
    suppressed the technical pillar and capped every score at 50-65.

    This asserts the structural fix - the scoring functions have no parameter
    that could carry a narrative string. If someone later adds one, this test
    fails and the review conversation happens before the bug ships.
    """
    text_free = ("score_option_liquidity", "score_spot_liquidity",
                 "score_technical", "score_sentiment")
    for name in text_free:
        sig = inspect.signature(getattr(scoring, name))
        for pname, param in sig.parameters.items():
            assert param.annotation is not str, (
                f"{name}({pname}) takes a string. Scoring must stay numeric."
            )


def test_scoring_module_does_not_import_the_llm():
    """
    The other half of the structural fix: scoring cannot call a model even
    indirectly, because it does not import the module that can.

    Checked against the parsed import graph rather than the source text, so
    the word "LLM" in a comment does not trip it and a real
    `from app.llm import ...` cannot hide behind an alias.
    """
    import ast

    tree = ast.parse(inspect.getsource(scoring))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {m for m in imported if "llm" in m.split(".")}
    assert not forbidden, f"scoring must not import the LLM layer: {forbidden}"
