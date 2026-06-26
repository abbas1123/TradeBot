"""DonchianFuturesStrategy — long AND short trend-following for the futures engine.

Symmetric Donchian/Turtle breakout:
  - LONG  when close > EMA200 (up regime) and close > prior `entry`-day high.
  - SHORT when close < EMA200 (down regime) and close < prior `entry`-day low.
Exits:
  - long  -> SELL  on close < prior `exit`-day low,  or regime flip (close < EMA200)
  - short -> COVER on close > prior `exit`-day high, or regime flip (close > EMA200)
Initial ATR stop is carried on the Signal and enforced intrabar by the engine.
"""
from __future__ import annotations

import pandas as pd

from ..indicators.indicators import atr, donchian_high, donchian_low, ema
from .base import Action, PositionState, Signal, Strategy


class DonchianFuturesStrategy(Strategy):
    timeframe = "1d"

    def __init__(self, params):
        super().__init__(params)
        self.warmup_bars = max(self.p.ema_trend, self.p.donchian_entry, self.p.atr_period) + 5

    def generate_signal(self, df: pd.DataFrame, position: PositionState) -> Signal:
        close = df["close"]
        last_ts = df["timestamp"].iloc[-1] if "timestamp" in df.columns else df.index[-1]
        c = float(close.iloc[-1])

        ema200 = float(ema(close, self.p.ema_trend).iloc[-1])
        hi_entry = donchian_high(df["high"], self.p.donchian_entry).iloc[-1]
        lo_entry = donchian_low(df["low"], self.p.donchian_entry).iloc[-1]
        hi_exit = donchian_high(df["high"], self.p.donchian_exit).iloc[-1]
        lo_exit = donchian_low(df["low"], self.p.donchian_exit).iloc[-1]
        atr_v = atr(df, self.p.atr_period).iloc[-1]
        ind = {"ema200": ema200, "hi_entry": hi_entry, "lo_entry": lo_entry, "atr": atr_v}

        if any(pd.isna(x) for x in (ema200, hi_entry, lo_entry, hi_exit, lo_exit, atr_v)):
            return Signal(Action.HOLD, "warmup/NaN", last_ts, c, indicators=ind)

        hi_entry, lo_entry = float(hi_entry), float(lo_entry)
        hi_exit, lo_exit, atr_v = float(hi_exit), float(lo_exit), float(atr_v)
        mult = self.p.atr_stop_mult

        if position.is_flat:
            if c > ema200 and c > hi_entry:
                return Signal(Action.BUY, f"long breakout {c:.2f} > {hi_entry:.2f}", last_ts, c,
                              stop_price=c - mult * atr_v, atr=atr_v, indicators=ind)
            if c < ema200 and c < lo_entry:
                return Signal(Action.SHORT, f"short breakdown {c:.2f} < {lo_entry:.2f}", last_ts, c,
                              stop_price=c + mult * atr_v, atr=atr_v, indicators=ind)
            return Signal(Action.HOLD, "no breakout", last_ts, c, atr=atr_v, indicators=ind)

        if position.is_long:
            if c < ema200:
                return Signal(Action.SELL, f"regime flip {c:.2f} < EMA200 {ema200:.2f}", last_ts, c, atr=atr_v, indicators=ind)
            if c < lo_exit:
                return Signal(Action.SELL, f"long trailing exit {c:.2f} < {lo_exit:.2f}", last_ts, c, atr=atr_v, indicators=ind)
            return Signal(Action.HOLD, "long intact", last_ts, c, atr=atr_v, indicators=ind)

        # short
        if c > ema200:
            return Signal(Action.COVER, f"regime flip {c:.2f} > EMA200 {ema200:.2f}", last_ts, c, atr=atr_v, indicators=ind)
        if c > hi_exit:
            return Signal(Action.COVER, f"short trailing exit {c:.2f} > {hi_exit:.2f}", last_ts, c, atr=atr_v, indicators=ind)
        return Signal(Action.HOLD, "short intact", last_ts, c, atr=atr_v, indicators=ind)
