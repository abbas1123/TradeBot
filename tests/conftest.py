"""Shared pytest fixtures: synthetic, hand-checkable OHLCV and param bundles."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest


@pytest.fixture
def make_ohlcv():
    """Build an OHLCV DataFrame from lists. Missing H/L/O default to the close
    (a flat bar), so callers control exactly the prices that matter for a test."""

    def _make(closes, highs=None, lows=None, opens=None, vol=1.0, start="2020-01-01"):
        n = len(closes)
        ts = pd.date_range(start=start, periods=n, freq="D", tz="UTC")
        closes = list(map(float, closes))
        opens = list(map(float, opens)) if opens is not None else list(closes)
        highs = list(map(float, highs)) if highs is not None else list(closes)
        lows = list(map(float, lows)) if lows is not None else list(closes)
        return pd.DataFrame(
            {
                "timestamp": ts,
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": [float(vol)] * n,
            }
        )

    return _make


@pytest.fixture
def strat_params():
    """Small indicator periods so fixtures stay tiny and hand-checkable."""
    return SimpleNamespace(
        ema_trend=5,
        donchian_entry=3,
        donchian_exit=2,
        atr_period=3,
        atr_stop_mult=2.0,
        rsi_period=3,
        rsi_buy=35,
        rsi_exit=55,
        reward_risk=1.5,
        bb_period=4,
        bb_std=2.0,
        chop_period=3,
        chop_threshold=50.0,
    )


@pytest.fixture
def risk_settings():
    return SimpleNamespace(
        live_capital_cap=10_000.0,
        risk_per_trade_pct=1.0,
        max_open_positions=2,
        max_daily_loss_pct=5.0,
        max_consecutive_errors=5,
    )
