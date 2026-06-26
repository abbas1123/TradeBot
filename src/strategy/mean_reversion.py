"""MeanReversionStrategy — Bollinger-band reversion for RANGING markets (long & short).

The range-harvesting counterpart to trend-following (the research's "right tool for a
sideways regime"): fade extremes back toward the mean.
  - FLAT: BUY when close < lower band (oversold); SHORT when close > upper band.
  - exit at the mean (middle band); ATR stop beyond the band guards a range break.
Mechanically similar to what a grid bot harvests, but as a single risk-managed position.
"""
from __future__ import annotations

import pandas as pd

from ..indicators.indicators import atr, bollinger
from .base import Action, PositionState, Signal, Strategy


class MeanReversionStrategy(Strategy):
    timeframe = "1d"

    def __init__(self, params):
        super().__init__(params)
        self.warmup_bars = max(self.p.bb_period, self.p.atr_period) + 5

    def generate_signal(self, df: pd.DataFrame, position: PositionState) -> Signal:
        close = df["close"]
        last_ts = df["timestamp"].iloc[-1] if "timestamp" in df.columns else df.index[-1]
        c = float(close.iloc[-1])

        upper, mid, lower = bollinger(close, self.p.bb_period, self.p.bb_std)
        u, m, lo = upper.iloc[-1], mid.iloc[-1], lower.iloc[-1]
        atr_v = atr(df, self.p.atr_period).iloc[-1]
        ind = {"bb_upper": u, "bb_mid": m, "bb_lower": lo, "atr": atr_v}

        if any(pd.isna(x) for x in (u, m, lo, atr_v)):
            return Signal(Action.HOLD, "warmup/NaN", last_ts, c, indicators=ind)

        u, m, lo, atr_v = float(u), float(m), float(lo), float(atr_v)
        mult = self.p.atr_stop_mult

        if position.is_flat:
            if c < lo:
                return Signal(Action.BUY, f"oversold {c:.2f} < lower {lo:.2f}", last_ts, c,
                              stop_price=c - mult * atr_v, target_price=m, atr=atr_v, indicators=ind)
            if c > u:
                return Signal(Action.SHORT, f"overbought {c:.2f} > upper {u:.2f}", last_ts, c,
                              stop_price=c + mult * atr_v, target_price=m, atr=atr_v, indicators=ind)
            return Signal(Action.HOLD, "inside bands", last_ts, c, atr=atr_v, indicators=ind)

        if position.is_long:
            if c >= m:
                return Signal(Action.SELL, f"reverted to mean {c:.2f} >= {m:.2f}", last_ts, c, atr=atr_v, indicators=ind)
            return Signal(Action.HOLD, "holding long to mean", last_ts, c, atr=atr_v, indicators=ind)

        # short
        if c <= m:
            return Signal(Action.COVER, f"reverted to mean {c:.2f} <= {m:.2f}", last_ts, c, atr=atr_v, indicators=ind)
        return Signal(Action.HOLD, "holding short to mean", last_ts, c, atr=atr_v, indicators=ind)
