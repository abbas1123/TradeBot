"""Pure indicator functions: EMA, RSI (Wilder), ATR (Wilder), Donchian channels.

Each function is pure (does not mutate its input) and returns a pd.Series aligned to
the input index. No trading logic lives here. Implemented in plain pandas/numpy to
avoid the pandas-ta / numpy 2.x incompatibility and to keep everything unit-testable
against hand-computed values.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

OHLCV_COLS = ["timestamp", "open", "high", "low", "close", "volume"]


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, recursive form (alpha = 2/(period+1)).

    adjust=False gives the recursive EMA traders expect; the first value equals the
    first price (warm-up transient that decays exponentially).
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    return series.ewm(span=period, adjust=False).mean()


def _wilder_smooth(values: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing: SMA-seeded recursive average with alpha = 1/period.

    The first `period` outputs are NaN; output at index `period-1` (counting from the
    first non-NaN input) is the SMA seed, then the recursion
    avg_t = (avg_{t-1}*(period-1) + value_t) / period.
    Matches the RSI/ATR reference values used by TradingView/ta.
    """
    arr = values.to_numpy(dtype="float64")
    n = arr.shape[0]
    out = np.full(n, np.nan, dtype="float64")
    # find first index where we have `period` consecutive non-NaN values to seed
    # (values series here already has a leading NaN from .diff()/.shift())
    first_valid = values.first_valid_index()
    if first_valid is None:
        return pd.Series(out, index=values.index)
    start = values.index.get_loc(first_valid)
    seed_end = start + period  # need `period` values [start, start+period)
    if seed_end > n:
        return pd.Series(out, index=values.index)
    out[seed_end - 1] = np.nanmean(arr[start:seed_end])
    for i in range(seed_end, n):
        out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
    return pd.Series(out, index=values.index)


def rsi(series: pd.Series, period: int) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing.

    First `period` values are NaN (warm-up). All-up series -> 100, all-down -> 0.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _wilder_smooth(gain, period)
    avg_loss = _wilder_smooth(loss, period)
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    # avg_loss == 0 (pure uptrend) -> rs = inf -> out -> 100; ensure that, not NaN
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    # avg_gain == 0 (pure downtrend) -> rs = 0 -> out = 0 (already correct)
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    """True range: max(H-L, |H-prevC|, |L-prevC|). TR_0 = H_0 - L_0 (prevC is NaN)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)  # skipna=True by default -> TR_0 = H_0 - L_0
    return tr


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range using Wilder's smoothing of true range."""
    if period < 1:
        raise ValueError("period must be >= 1")
    tr = true_range(df)
    return _wilder_smooth(tr, period)


def donchian_high(high: pd.Series, period: int, shift: int = 1) -> pd.Series:
    """Highest high of the PRIOR `period` bars (current bar excluded via shift)."""
    if period < 1:
        raise ValueError("period must be >= 1")
    return high.rolling(window=period).max().shift(shift)


def donchian_low(low: pd.Series, period: int, shift: int = 1) -> pd.Series:
    """Lowest low of the PRIOR `period` bars (current bar excluded via shift)."""
    if period < 1:
        raise ValueError("period must be >= 1")
    return low.rolling(window=period).min().shift(shift)


def bollinger(series: pd.Series, period: int, num_std: float = 2.0):
    """Bollinger bands: (upper, mid, lower) = SMA ± num_std * population stdev."""
    if period < 1:
        raise ValueError("period must be >= 1")
    mid = series.rolling(window=period).mean()
    sd = series.rolling(window=period).std(ddof=0)
    return mid + num_std * sd, mid, mid - num_std * sd


def choppiness_index(df: pd.DataFrame, period: int) -> pd.Series:
    """Choppiness Index in [0,100]. LOW (<~38) = trending; HIGH (>~62) = choppy/ranging.

    CI = 100 * log10( sum(TR, n) / (maxHigh(n) - minLow(n)) ) / log10(n)
    """
    if period < 2:
        raise ValueError("period must be >= 2")
    tr = true_range(df)
    tr_sum = tr.rolling(window=period).sum()
    hi = df["high"].rolling(window=period).max()
    lo = df["low"].rolling(window=period).min()
    rng = (hi - lo).replace(0.0, np.nan)
    return 100.0 * np.log10(tr_sum / rng) / np.log10(period)


# --- convenience wrappers matching the spec's df-based signatures ---
def ema_col(df: pd.DataFrame, period: int, col: str = "close") -> pd.Series:
    return ema(df[col], period)


def rsi_col(df: pd.DataFrame, period: int, col: str = "close") -> pd.Series:
    return rsi(df[col], period)
