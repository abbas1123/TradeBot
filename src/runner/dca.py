"""DCA engine — periodically buys a fixed budget split across a basket and HOLDS.

This is the data-backed approach: in backtests, DCA and buy-and-hold beat the trading bot
on the same coins/window (the trading bot bled fees on 1h and was ~flat on 1d). DCA deploys
a lump sum evenly over time to reduce timing risk, then just holds.

It exposes the SAME read surface as PortfolioEngine (equity/cash/positions/marks/trades/
events/ledger/...), so the existing web dashboard + Monitor + Telegram work unchanged.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace

from ..backtest.backtester import BTConfig, Trade  # noqa: F401 (Trade kept for surface parity)
from ..utils.ledger import append_ledger
from .engine import LevPosition


class DCAEngine:
    """Accumulate-and-hold across a basket. Buys one tranche per scheduled period."""

    def __init__(self, symbols, cfg: BTConfig, n_buys: int = 52, ledger_path: str | None = None):
        self.symbols = list(symbols)
        self.cfg = cfg
        self.cash = cfg.initial_capital
        self.n_buys = max(1, int(n_buys))          # split the lump sum into this many buys
        self.per_buy = cfg.initial_capital / self.n_buys
        self.buys_done = 0
        self.positions: dict[str, LevPosition] = {}  # sym -> holding (avg cost, total units)
        self.marks: dict[str, float] = {}
        self.trades: list = []                      # DCA never sells; kept for dashboard parity
        self.realized_pnl = 0.0
        self.funding_total = 0.0
        self.liquidations = 0
        self.leverage = 1
        self.events: deque[str] = deque(maxlen=400)
        self.ledger: deque[dict] = deque(maxlen=200)
        self.ledger_path = ledger_path
        self.last_signals: dict = {}
        self.pending: dict = {}
        self.risk = SimpleNamespace(s=SimpleNamespace(max_open_positions=len(self.symbols)))
        self.lock = threading.RLock()

    # --- read surface (matches PortfolioEngine, so the dashboard is reused as-is) ---
    def equity(self) -> float:
        eq = self.cash
        for sym, p in list(self.positions.items()):
            mark = self.marks.get(sym, p.entry_price)
            eq += p.im + p.unrealized(mark)
        return eq

    def unrealized(self) -> float:
        return sum(p.unrealized(self.marks.get(s, p.entry_price)) for s, p in list(self.positions.items()))

    def total_return(self) -> float:
        return self.equity() / self.cfg.initial_capital - 1.0

    def set_mark(self, symbol, price):
        if price and price > 0:
            self.marks[symbol] = float(price)

    def update_mark(self, symbol, price):
        with self.lock:
            self.set_mark(symbol, price)

    def _log(self, msg: str):
        self.events.append(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S  ") + msg)

    # --- DCA action ---
    def invest_round(self, ts) -> bool:
        """Spend one tranche: split per_buy equally across symbols that have a price; HOLD."""
        with self.lock:
            if self.buys_done >= self.n_buys:
                return False
            live = [s for s in self.symbols if self.marks.get(s, 0) > 0]
            if not live:
                return False
            spend_each = self.per_buy / len(live)
            fee_rate = self.cfg.fee_rate
            for sym in live:
                price = self.marks[sym]
                if spend_each <= 0 or spend_each > self.cash + 1e-9:
                    continue
                fee = spend_each * fee_rate
                qty = (spend_each - fee) / price
                self.cash -= spend_each
                p = self.positions.get(sym)
                if p is None:
                    self.positions[sym] = LevPosition(
                        sym, "LONG", price, qty, spend_each - fee, 1.0, 0.0, None, ts, fee, 0.0, peak=price)
                else:  # add to the holding, recompute average cost
                    nq = p.qty + qty
                    p.entry_price = (p.entry_price * p.qty + price * qty) / nq
                    p.qty = nq
                    p.im += (spend_each - fee)
                    p.entry_fee += fee
            self.buys_done += 1
            self._log(f"DCA buy #{self.buys_done}/{self.n_buys}: {self.per_buy:.2f} across {len(live)} coins · equity {self.equity():,.2f}")
            self._record_ledger(ts)
            return True

    def _record_ledger(self, ts):
        rec = {
            "ts": str(ts), "event": "BUY", "symbol": "BASKET", "side": "DCA",
            "price": 0.0, "qty": 0.0, "equity": round(self.equity(), 2), "cash": round(self.cash, 2),
            "realized_pnl": 0.0, "unrealized_pnl": round(self.unrealized(), 2),
            "buys_done": self.buys_done, "source": "dca",
        }
        self.ledger.append(rec)
        append_ledger(self.ledger_path, rec)

    def step(self, symbol, df):  # no-op: DCA is schedule-driven, not signal-driven
        return
