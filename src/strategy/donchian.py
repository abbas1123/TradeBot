"""DonchianTrendStrategy — the default trend-following model (recommended-model.md §4).

Long-or-flat. Regime filter (close > EMA200) + 20-day breakout entry; trailing exit on
10-day breakdown; hard exit on regime flip below EMA200. The initial ATR stop is carried
on the Signal and enforced intrabar by the executor (see exits.check_level_exit).
"""
from __future__ import annotations

import pandas as pd

from ..indicators.indicators import atr, donchian_high, donchian_low, ema
from .base import Action, PositionState, Signal, Strategy


class DonchianTrendStrategy(Strategy):
    timeframe = "1d"

    def __init__(self, params):
        super().__init__(params)
        self.warmup_bars = (
            max(self.p.ema_trend, self.p.donchian_entry, self.p.atr_period) + 5
        )

    def generate_signal(self, df: pd.DataFrame, position: PositionState) -> Signal:
        close = df["close"]
        last_ts = df["timestamp"].iloc[-1] if "timestamp" in df.columns else df.index[-1]
        c = float(close.iloc[-1])

        ema200 = float(ema(close, self.p.ema_trend).iloc[-1])
        dc_hi = donchian_high(df["high"], self.p.donchian_entry).iloc[-1]
        dc_lo = donchian_low(df["low"], self.p.donchian_exit).iloc[-1]
        atr_v = atr(df, self.p.atr_period).iloc[-1]
        ind = {"ema200": ema200, "dc_hi": dc_hi, "dc_lo": dc_lo, "atr": atr_v}

        # warm-up / NaN -> never trade
        if any(pd.isna(x) for x in (ema200, dc_hi, dc_lo, atr_v)):
            return Signal(Action.HOLD, "warmup/NaN", last_ts, c, indicators=ind)

        dc_hi = float(dc_hi)
        dc_lo = float(dc_lo)
        atr_v = float(atr_v)

        if position.is_flat:
            regime_ok = c > ema200
            breakout = c > dc_hi
            if regime_ok and breakout:
                stop = c - self.p.atr_stop_mult * atr_v
                return Signal(
                    Action.BUY,
                    f"20d breakout {c:.2f} > {dc_hi:.2f}, regime ON",
                    last_ts,
                    c,
                    stop_price=stop,
                    target_price=None,
                    atr=atr_v,
                    indicators=ind,
                )
            return Signal(
                Action.HOLD, "no breakout / regime off", last_ts, c, atr=atr_v, indicators=ind
            )

        # LONG -> check exits (regime flip reported first for log clarity)
        if c < ema200:
            return Signal(
                Action.SELL,
                f"regime flip: close {c:.2f} < EMA200 {ema200:.2f}",
                last_ts,
                c,
                atr=atr_v,
                indicators=ind,
            )
        if c < dc_lo:
            return Signal(
                Action.SELL,
                f"10d trailing exit: close {c:.2f} < {dc_lo:.2f}",
                last_ts,
                c,
                atr=atr_v,
                indicators=ind,
            )
        return Signal(Action.HOLD, "trend intact, holding", last_ts, c, atr=atr_v, indicators=ind)
