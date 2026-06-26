"""RegimeSwitchStrategy — the research's core takeaway: use the right tool per regime.

Classifies each bar with the Choppiness Index: trending markets get the Donchian
breakout (trend-following), ranging markets get Bollinger mean-reversion. This combines
the two approaches the evidence says a small account can realistically run, instead of
forcing one strategy through every regime.
"""
from __future__ import annotations

from dataclasses import replace

import pandas as pd

from ..indicators.indicators import choppiness_index
from .base import PositionState, Signal, Strategy
from .donchian_futures import DonchianFuturesStrategy
from .mean_reversion import MeanReversionStrategy


class RegimeSwitchStrategy(Strategy):
    timeframe = "1d"

    def __init__(self, params):
        super().__init__(params)
        self.trend = DonchianFuturesStrategy(params)
        self.mr = MeanReversionStrategy(params)
        self.warmup_bars = max(self.trend.warmup_bars, self.mr.warmup_bars, self.p.chop_period + 5)
        self.last_regime = "?"

    def generate_signal(self, df: pd.DataFrame, position: PositionState) -> Signal:
        ci = choppiness_index(df, self.p.chop_period).iloc[-1]
        if pd.isna(ci):
            # not enough data to classify -> default to trend logic (handles its own warmup)
            return self.trend.generate_signal(df, position)
        trending = float(ci) < self.p.chop_threshold
        self.last_regime = "trend" if trending else "range"
        sub = self.trend if trending else self.mr
        sig = sub.generate_signal(df, position)
        return replace(sig, reason=f"[{self.last_regime} CI={float(ci):.0f}] {sig.reason}")
