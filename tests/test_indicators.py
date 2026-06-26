"""Indicator tests against hand-computed values."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.indicators.indicators import (
    atr,
    bollinger,
    choppiness_index,
    donchian_high,
    donchian_low,
    ema,
    rsi,
    true_range,
)


def test_ema_recursive():
    # alpha = 2/(3+1) = 0.5, seeded at the first value (adjust=False)
    s = pd.Series([1, 2, 3, 4, 5], dtype="float64")
    out = ema(s, 3).to_numpy()
    expected = [1.0, 1.5, 2.25, 3.125, 4.0625]
    assert np.allclose(out, expected)


def test_ema_constant():
    s = pd.Series([5.0, 5.0, 5.0, 5.0])
    assert np.allclose(ema(s, 4).to_numpy(), [5, 5, 5, 5])


def test_true_range_with_gap():
    # bar 1 gaps up: low(20) above prev close(10) -> TR driven by |low - prevClose|
    df = pd.DataFrame(
        {
            "high": [10.0, 25.0],
            "low": [8.0, 20.0],
            "close": [10.0, 22.0],
        }
    )
    tr = true_range(df).to_numpy()
    assert tr[0] == 2.0  # H-L (prev close is NaN)
    assert tr[1] == 15.0  # max(25-20=5, |25-10|=15, |20-10|=10)


def test_atr_constant_range_converges():
    n = 10
    df = pd.DataFrame(
        {
            "high": [105.0] * n,
            "low": [95.0] * n,
            "close": [100.0] * n,
        }
    )
    a = atr(df, 3)
    assert pd.isna(a.iloc[0]) and pd.isna(a.iloc[1])
    assert np.isclose(a.iloc[-1], 10.0)
    assert np.isclose(a.iloc[2], 10.0)  # SMA seed of three TRs all == 10


def test_rsi_all_up_is_100():
    s = pd.Series(np.arange(1, 21, dtype="float64"))
    r = rsi(s, 14)
    assert np.isclose(r.iloc[-1], 100.0)


def test_rsi_all_down_is_0():
    s = pd.Series(np.arange(20, 0, -1, dtype="float64"))
    r = rsi(s, 14)
    assert np.isclose(r.iloc[-1], 0.0)


def test_rsi_bounds_and_warmup():
    s = pd.Series([10, 11, 9, 12, 8, 13, 7, 14, 6, 15, 5, 16, 4, 17, 3, 18], dtype="float64")
    r = rsi(s, 14)
    # first `period` values are NaN (warm-up)
    assert r.iloc[:14].isna().all()
    assert not pd.isna(r.iloc[14])
    valid = r.dropna()
    assert ((valid >= 0) & (valid <= 100)).all()


def test_donchian_high_shift_excludes_current_bar():
    high = pd.Series([1, 2, 3, 4, 5, 6], dtype="float64")
    dc = donchian_high(high, 3)
    # prior-3 high, current bar excluded -> [nan,nan,nan,3,4,5] (NOT ...,5,6)
    expected = [np.nan, np.nan, np.nan, 3.0, 4.0, 5.0]
    assert np.array_equal(dc.to_numpy(), np.array(expected), equal_nan=True)


def test_donchian_low_shift_excludes_current_bar():
    low = pd.Series([6, 5, 4, 3, 2, 1], dtype="float64")
    dc = donchian_low(low, 3)
    expected = [np.nan, np.nan, np.nan, 4.0, 3.0, 2.0]
    assert np.array_equal(dc.to_numpy(), np.array(expected), equal_nan=True)


def test_bollinger_constant_series_zero_width():
    s = pd.Series([10.0] * 6)
    u, m, lo = bollinger(s, 4, 2.0)
    assert u.iloc[-1] == m.iloc[-1] == lo.iloc[-1] == 10.0


def test_bollinger_known_values():
    s = pd.Series([1, 2, 3, 4], dtype="float64")
    u, m, lo = bollinger(s, 4, 2.0)  # mean 2.5, pop std sqrt(1.25)=1.1180
    assert m.iloc[-1] == pytest.approx(2.5)
    assert u.iloc[-1] == pytest.approx(2.5 + 2 * 1.1180, abs=1e-3)
    assert lo.iloc[-1] == pytest.approx(2.5 - 2 * 1.1180, abs=1e-3)


def test_choppiness_trending_low_choppy_high():
    # steady uptrend -> low CI (trending)
    trend_close = [100, 110, 120, 130, 140, 150]
    trend = pd.DataFrame({"high": trend_close, "low": trend_close, "close": trend_close})
    ci_trend = choppiness_index(trend, 3).iloc[-1]
    # zig-zag -> high CI (choppy/ranging)
    chop_close = [100, 110, 100, 110, 100, 110]
    chop = pd.DataFrame({"high": chop_close, "low": chop_close, "close": chop_close})
    ci_chop = choppiness_index(chop, 3).iloc[-1]
    assert ci_trend < 50 < ci_chop
    assert 0 <= ci_trend <= 100 and 0 <= ci_chop <= 100
