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
        self._active = None  # the sub-strategy that owns the current open position
        self._regime = None  # current regime with hysteresis ("trend"/"range")

    def generate_signal(self, df: pd.DataFrame, position: PositionState) -> Signal:
        ci = choppiness_index(df, self.p.chop_period).iloc[-1]
        ci_v = None if pd.isna(ci) else float(ci)
        thr = self.p.chop_threshold
        band = getattr(self.p, "chop_band", 0.0)
        # hysteresis: only flip regime once CI clears the band; inside the band, hold the
        # current regime so a price hovering at the threshold can't churn trend<->range
        if ci_v is None or ci_v < thr - band:
            self._regime = "trend"
        elif ci_v > thr + band:
            self._regime = "range"
        elif self._regime is None:
            self._regime = "trend"
        regime_sub = self.trend if self._regime == "trend" else self.mr

        if position.is_flat:
            self._active = regime_sub  # whoever is in charge when an entry is decided
            sub = regime_sub
        else:
            # an open position is managed by the SAME sub that opened it, so its exit rules
            # stay coherent even if the regime flips mid-trade
            sub = self._active or regime_sub

        self.last_regime = "trend" if sub is self.trend else "range"
        sig = sub.generate_signal(df, position)
        tag = self.last_regime + ("" if ci_v is None else f" CI={ci_v:.0f}")
        return replace(sig, reason=f"[{tag}] {sig.reason}")
