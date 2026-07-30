"""SPY market-regime classifier — pure function, no I/O."""
from app.engine.regime import MarketRegime, blocks_direction, classify_spy_regime


def _uptrend(n: int = 30, start: float = 100.0, step: float = 0.5) -> list[float]:
    return [start + i * step for i in range(n)]


def _downtrend(n: int = 30, start: float = 100.0, step: float = 0.5) -> list[float]:
    return [start - i * step for i in range(n)]


def test_risk_on_when_above_ma_and_rising():
    closes = _uptrend(30)
    assert classify_spy_regime(closes) is MarketRegime.RISK_ON


def test_risk_off_when_below_ma():
    closes = _downtrend(30)
    assert classify_spy_regime(closes) is MarketRegime.RISK_OFF


def test_risk_off_when_above_ma_but_slope_negative():
    # Climb for 25 days, then sell off for 5 — still may be near MA.
    closes = _uptrend(25, start=100.0, step=1.0)
    # Sharp drop last 5 bars from peak
    peak = closes[-1]
    closes = closes + [peak - 2, peak - 4, peak - 6, peak - 8, peak - 10]
    # Ensure we have slope negative over lookback 5
    reg = classify_spy_regime(closes, ma_period=20, slope_lookback=5)
    assert reg is MarketRegime.RISK_OFF


def test_unknown_with_insufficient_history():
    assert classify_spy_regime([100.0, 101.0]) is MarketRegime.UNKNOWN


def test_blocks_long_in_risk_off():
    assert blocks_direction(MarketRegime.RISK_OFF, "LONG_CALL")
    assert blocks_direction(MarketRegime.RISK_OFF, "LONG_SHARE")
    assert blocks_direction(MarketRegime.RISK_OFF, "LONG_PUT") is None


def test_blocks_put_in_risk_on():
    assert blocks_direction(MarketRegime.RISK_ON, "LONG_PUT")
    assert blocks_direction(MarketRegime.RISK_ON, "LONG_CALL") is None
    assert blocks_direction(MarketRegime.RISK_ON, "LONG_SHARE") is None


def test_unknown_never_blocks():
    assert blocks_direction(MarketRegime.UNKNOWN, "LONG_CALL") is None
    assert blocks_direction(MarketRegime.UNKNOWN, "LONG_PUT") is None
