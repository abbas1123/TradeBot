"""Persistent bot state for the live runner (atomic JSON).

Survives restarts so the bot never loses track of open positions, the kill switch, or the
realized daily PnL. The daily PnL resets at UTC midnight; a kill switch tripped by the
daily-loss limit does NOT auto-reset (an operator must look before resuming).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Position:
    symbol: str
    quantity: float
    entry_price: float
    stop_price: float
    opened_at: str  # ISO UTC


@dataclass
class BotState:
    open_positions: dict = field(default_factory=dict)  # symbol -> Position dict
    realized_daily_pnl: float = 0.0
    pnl_day: str = ""  # UTC date the pnl belongs to
    kill_switch_active: bool = False
    kill_switch_reason: str = ""
    consecutive_errors: int = 0
    last_processed_ts: dict = field(default_factory=dict)  # symbol -> ISO ts
    cumulative_pnl: float = 0.0  # realized PnL since inception (for max-drawdown breaker)
    peak_pnl: float = 0.0  # high-water mark of cumulative_pnl


class StateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.state = self._load()

    def _load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            st = BotState(**{k: data.get(k, v) for k, v in asdict(BotState()).items()})
        except Exception:
            st = BotState()
        self._maybe_reset_daily(st)
        return st

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic

    def _maybe_reset_daily(self, st: BotState):
        today = datetime.now(timezone.utc).date().isoformat()
        if st.pnl_day != today:
            st.realized_daily_pnl = 0.0
            st.pnl_day = today
            # NB: kill switch is NOT auto-cleared on a new day — manual reset required

    def reset_daily_if_needed(self):
        self._maybe_reset_daily(self.state)
