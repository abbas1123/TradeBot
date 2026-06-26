"""ConfluenceStrategy — the alternative mean-reversion-in-trend model (spec §4.4).

Buys pullbacks inside an uptrend (close > EMA200 and RSI < RSI_BUY), exits on RSI
strength (RSI > RSI_EXIT), with an ATR stop and a reward/risk take-profit. Native
timeframe is 1h. Kept behind the same Strategy interface for backtest comparison.
"""
from __future__ import annotations

import pandas as pd

from ..indicators.indicators import atr, ema, rsi
from .base import Action, PositionState, Signal, Strategy


class ConfluenceStrategy(Strategy):
    timeframe = "1h"

    def __init__(self, params):
        super().__init__(params)
        self.warmup_bars = max(self.p.ema_trend, self.p.rsi_period, self.p.atr_period) + 5

    def generate_signal(self, df: pd.DataFrame, position: PositionState) -> Signal:
        close = df["close"]
        last_ts = df["timestamp"].iloc[-1] if "timestamp" in df.columns else df.index[-1]
        c = float(close.iloc[-1])

        ema200 = float(ema(close, self.p.ema_trend).iloc[-1])
        rsi_v = rsi(close, self.p.rsi_period).iloc[-1]
        atr_v = atr(df, self.p.atr_period).iloc[-1]
        ind = {"ema200": ema200, "rsi": rsi_v, "atr": atr_v}

        if any(pd.isna(x) for x in (ema200, rsi_v, atr_v)):
            return Signal(Action.HOLD, "warmup/NaN", last_ts, c, indicators=ind)

        rsi_v = float(rsi_v)
        atr_v = float(atr_v)

        if position.is_flat:
            if c > ema200 and rsi_v < self.p.rsi_buy:
                stop = c - self.p.atr_stop_mult * atr_v
                risk = c - stop
                target = c + self.p.reward_risk * risk
                return Signal(
                    Action.BUY,
                    f"pullback: RSI {rsi_v:.1f} < {self.p.rsi_buy} in uptrend",
                    last_ts,
                    c,
                    stop_price=stop,
                    target_price=target,
                    atr=atr_v,
                    indicators=ind,
                )
            return Signal(Action.HOLD, "no setup", last_ts, c, atr=atr_v, indicators=ind)

        # LONG -> exit on RSI strength; stop/target enforced by executor
        if rsi_v > self.p.rsi_exit:
            return Signal(
                Action.SELL,
                f"RSI exit {rsi_v:.1f} > {self.p.rsi_exit}",
                last_ts,
                c,
                atr=atr_v,
                indicators=ind,
            )
        return Signal(Action.HOLD, "holding", last_ts, c, atr=atr_v, indicators=ind)
