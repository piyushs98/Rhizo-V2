"""EMA crossover pure functions."""
from app.engine.crossover import detect_crosses, ema_series


def test_ema_series_length():
    xs = [float(i) for i in range(1, 31)]
    e = ema_series(xs, 9)
    assert len(e) == 30
    assert e[7] is None
    assert e[8] is not None


def test_detect_bull_cross():
    # flat then jump: fast will cross above slow
    xs = [100.0] * 60 + [100.0 + i * 2 for i in range(40)]
    crosses = detect_crosses(xs, 9, 21)
    assert any(d == 1 for _, d in crosses)


def test_fast_must_be_slower_period():
    import pytest
    with pytest.raises(ValueError):
        detect_crosses([1.0] * 50, 50, 21)
