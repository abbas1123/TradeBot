"""Strategy interface and shared value objects.

A Signal carries everything the backtester AND the live runner need so the same
generate_signal() code drives both with no duplicated decision logic. Anti-lookahead
is structural: the caller passes only CLOSED bars, the strategy reads df.iloc[-1] as
"now", and the action happens on the NEXT bar (the strategy never sees the fill).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum

import pandas as pd


class Action(str, Enum):
    BUY = "buy"  # enter long   (FLAT -> LONG)
    SELL = "sell"  # exit long    (LONG -> FLAT)
    SHORT = "short"  # enter short  (FLAT -> SHORT)  [futures only]
    COVER = "cover"  # exit short   (SHORT -> FLAT)  [futures only]
    HOLD = "hold"  # do nothing


@dataclass(frozen=True)
class Signal:
    action: Action
    reason: str  # human-readable; makes "explain every trade" possible from logs alone
    bar_timestamp: pd.Timestamp  # the CLOSED bar this decision was made on
    bar_close: float  # close of that bar (reference price, NOT the fill price)
    stop_price: float | None = None  # initial hard stop (entry - mult*ATR)
    target_price: float | None = None  # take-profit (Confluence sets it; Donchian None)
    atr: float | None = None  # ATR at decision time, so executor can size the stop
    indicators: dict = field(default_factory=dict)  # snapshot for logging/plotting

    def to_dict(self) -> dict:
        d = asdict(self)
        d["action"] = self.action.value
        return d


@dataclass
class PositionState:
    state: str = "FLAT"  # "FLAT" | "LONG" | "SHORT"
    symbol: str | None = None
    entry_price: float | None = None
    quantity: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    entry_ts: pd.Timestamp | None = None

    @property
    def is_long(self) -> bool:
        return self.state == "LONG"

    @property
    def is_short(self) -> bool:
        return self.state == "SHORT"

    @property
    def is_flat(self) -> bool:
        return self.state == "FLAT"


class Strategy(ABC):
    """Every strategy implements generate_signal and declares its native timeframe."""

    timeframe: str = "1d"
    warmup_bars: int = 200

    def __init__(self, params):
        self.p = params  # a Settings instance or a SimpleNamespace of params

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame, position: PositionState) -> Signal:
        """df: OHLCV with ALL bars CLOSED; df.iloc[-1] is the most recent closed bar.
        position: current LONG/FLAT state, so entry vs exit can be decided here.
        Returns a Signal describing what to do on the NEXT bar. Must be deterministic.
        """
        raise NotImplementedError
