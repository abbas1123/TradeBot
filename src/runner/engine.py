"""SimEngine — stateful, bar-by-bar paper-trading engine (no API keys).

It applies the SAME decision logic as the backtester (entry filled on the next bar's
open, hard ATR stop checked intrabar via exits.check_level_exit, fees + slippage on every
fill, sizing via RiskManager) but one bar at a time, so it can drive both an accelerated
historical *replay* and a live *forward* simulation against real public prices with a
fake balance. State is persisted to JSON so a restart resumes where it left off.

This is also the shape the Phase 3 live runner will take — swap the simulated fill for a
real broker order and the loop is identical.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..backtest.backtester import BTConfig, Trade
from ..execution import futures
from ..strategy.base import Action, PositionState
from ..strategy.exits import check_level_exit
from ..utils.ledger import append_ledger


class SimEngine:
    def __init__(self, strategy, risk, cfg: BTConfig, symbol: str, state_path: str | None = None):
        self.strategy = strategy
        self.risk = risk
        self.cfg = cfg
        self.symbol = symbol
        self.state_path = Path(state_path) if state_path else None

        self.cash: float = cfg.initial_capital
        self.position = PositionState()
        self.entry_meta: dict = {}
        self.pending = None  # ("buy", Signal) | ("sell", reason)
        self.trades: list[Trade] = []
        self.realized_pnl: float = 0.0
        self.last_processed_ts: pd.Timestamp | None = None
        self.last_signal = None
        self.mark_price: float = 0.0
        self.events: deque[str] = deque(maxlen=200)

    # --- public API ------------------------------------------------------
    def step(self, history_df: pd.DataFrame):
        """Process the newest CLOSED bar (history_df.iloc[-1]). No-op if already seen."""
        if history_df is None or history_df.empty:
            return None
        newest = history_df.iloc[-1]
        ts = newest["timestamp"]
        if self.last_processed_ts is not None and ts <= self.last_processed_ts:
            return None  # bar already processed (idempotent)

        o, h, l, c = (
            float(newest["open"]),
            float(newest["high"]),
            float(newest["low"]),
            float(newest["close"]),
        )
        self.mark_price = c
        slip = self.cfg.slippage_bps / 1e4
        fee_rate = self.cfg.fee_rate

        # (1) fill any pending action from the previous bar, at THIS bar's open
        if self.pending is not None:
            kind = self.pending[0]
            if kind == "buy" and self.position.is_flat:
                sig = self.pending[1]
                fill = o * (1 + slip)
                sized = self.risk.size_position(
                    capital_available=self.cash,
                    entry_price=sig.bar_close,
                    stop_price=sig.stop_price,
                    min_notional=self.cfg.min_notional,
                    lot_step=self.cfg.lot_step,
                )
                if sized.approved:
                    cost = fill * sized.quantity
                    fee = cost * fee_rate
                    if cost + fee <= self.cash:
                        self.cash -= cost + fee
                        self.position = PositionState(
                            state="LONG",
                            symbol=self.symbol,
                            entry_price=fill,
                            quantity=sized.quantity,
                            stop_price=sig.stop_price,
                            target_price=sig.target_price,
                            entry_ts=ts,
                        )
                        self.entry_meta = {"entry_fee": fee, "entry_ts": ts}
                        self._log(f"BUY {sized.quantity:.6f} @ {fill:.2f}  stop {sig.stop_price:.2f}")
                    else:
                        self._log("BUY aborted: insufficient cash")
                else:
                    self._log(f"entry skipped: {sized.reason}")
            elif kind == "sell" and self.position.is_long:
                fill = o * (1 - slip)
                self._close(fill, ts, self.pending[1])
            self.pending = None

        # (2) manage open position: intrabar stop / target (resting order)
        if self.position.is_long:
            hit = check_level_exit(self.position, h, l)
            if hit is not None:
                reason, level = hit
                fill = (min(level, o) if reason == "stop" else max(level, o)) * (
                    1 - slip if reason == "stop" else 1 + slip
                )
                self._close(fill, ts, reason)

        # (3) decision on this CLOSED bar -> stage for the next bar's open
        sig = self.strategy.generate_signal(history_df, self.position)
        self.last_signal = sig
        if sig.action == Action.BUY and self.position.is_flat and self.pending is None:
            self.pending = ("buy", sig)
            self._log(f"signal BUY ({sig.reason}) -> will fill next bar open")
        elif sig.action == Action.SELL and self.position.is_long:
            self.pending = ("sell", f"signal:{sig.reason}")
            self._log(f"signal SELL ({sig.reason}) -> will fill next bar open")

        self.last_processed_ts = ts
        self.save()
        return sig

    # --- portfolio math --------------------------------------------------
    def equity(self, mark: float | None = None) -> float:
        m = mark if mark is not None else self.mark_price
        return self.cash + (self.position.quantity * m if self.position.is_long else 0.0)

    def unrealized(self, mark: float | None = None) -> float:
        if self.position.is_flat:
            return 0.0
        m = mark if mark is not None else self.mark_price
        return (m - self.position.entry_price) * self.position.quantity

    def total_return(self, mark: float | None = None) -> float:
        return self.equity(mark) / self.cfg.initial_capital - 1.0

    # --- internals -------------------------------------------------------
    def _close(self, fill_px: float, ts, reason: str):
        qty = self.position.quantity
        proceeds = fill_px * qty
        fee = proceeds * self.cfg.fee_rate
        self.cash += proceeds - fee
        entry_fee = self.entry_meta.get("entry_fee", 0.0)
        gross = (fill_px - self.position.entry_price) * qty
        pnl = gross - entry_fee - fee
        self.realized_pnl += pnl
        self.trades.append(
            Trade(
                entry_time=self.position.entry_ts,
                entry_price=self.position.entry_price,
                exit_time=ts,
                exit_price=fill_px,
                qty=qty,
                fees=entry_fee + fee,
                pnl=pnl,
                pnl_pct=pnl / (self.position.entry_price * qty) if qty else 0.0,
                bars_held=0,
                exit_reason=reason,
            )
        )
        self._log(f"SELL {qty:.6f} @ {fill_px:.2f}  pnl {pnl:+.2f} ({reason})")
        self.position = PositionState()
        self.entry_meta = {}

    def _log(self, msg: str):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.events.append(f"{stamp}  {msg}")

    # --- persistence (atomic JSON) --------------------------------------
    def save(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        pos = asdict(self.position)
        if pos.get("entry_ts") is not None:
            pos["entry_ts"] = pd.Timestamp(pos["entry_ts"]).isoformat()
        data = {
            "symbol": self.symbol,
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "initial_capital": self.cfg.initial_capital,
            "position": pos,
            "pending": self.pending[0] if self.pending else None,
            "last_processed_ts": self.last_processed_ts.isoformat() if self.last_processed_ts is not None else None,
            "num_trades": len(self.trades),
            "last_signal": ({"action": self.last_signal.action.value, "reason": self.last_signal.reason} if self.last_signal else None),
            "mark_price": self.mark_price,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def load(self) -> bool:
        if not self.state_path or not self.state_path.exists():
            return False
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        self.cash = data.get("cash", self.cash)
        self.realized_pnl = data.get("realized_pnl", 0.0)
        p = data.get("position") or {}
        if p.get("state") == "LONG":
            self.position = PositionState(
                state="LONG",
                symbol=p.get("symbol"),
                entry_price=p.get("entry_price"),
                quantity=p.get("quantity"),
                stop_price=p.get("stop_price"),
                target_price=p.get("target_price"),
                entry_ts=pd.Timestamp(p["entry_ts"]) if p.get("entry_ts") else None,
            )
        lpt = data.get("last_processed_ts")
        if lpt:
            self.last_processed_ts = pd.Timestamp(lpt)
        self.mark_price = data.get("mark_price", 0.0)
        return True


@dataclass
class LevPosition:
    symbol: str
    side: str  # "LONG" | "SHORT"
    entry_price: float
    qty: float
    im: float  # initial margin locked (= notional / leverage)
    leverage: float
    stop_price: float
    target_price: float | None
    entry_ts: object
    entry_fee: float
    liq: float  # precomputed liquidation price (maintenance-margin basis)
    funding_paid: float = 0.0
    peak: float = 0.0  # high-water mark since entry (max for long, min for short)
    atr_entry: float | None = None  # ATR at entry, for the chandelier trail
    init_stop: float | None = None  # original stop, defines 1R for the break-even move

    @property
    def notional0(self) -> float:
        return self.entry_price * self.qty

    def liq_price(self) -> float:
        return self.liq

    def unrealized(self, mark: float) -> float:
        return futures.unrealized_pnl(self.side, self.entry_price, self.qty, mark)

    def roe(self, mark: float) -> float:
        return self.unrealized(mark) / self.im if self.im else 0.0

    def margin_ratio(self, mark: float) -> float:
        return futures.margin_ratio(self.side, self.entry_price, self.qty, mark, self.im)


class PortfolioEngine:
    """Multi-symbol paper engine with optional leverage (futures-style margin).

    Shares one cash pool across symbols, sizes each entry by risk (capped by buying
    power = cash * leverage), locks margin = notional/leverage, and liquidates a position
    if price falls to entry*(1 - 1/leverage). Spot behaviour is leverage = 1. Reuses the
    same generate_signal() the backtester uses, per symbol.
    """

    def __init__(self, strategies: dict, risk, cfg: BTConfig, leverage: float = 1.0,
                 funding_rate: float = 0.0001, bar_hours: float = 24.0,
                 liq_fee_rate: float = 0.005, state_path: str | None = None,
                 ledger_path: str | None = None):
        self.strategies = strategies  # {symbol: Strategy}
        self.symbols = list(strategies.keys())
        self.risk = risk
        self.cfg = cfg
        self.leverage = max(1.0, float(leverage))
        self.funding_rate = funding_rate  # per 8h funding interval
        self.bar_hours = bar_hours
        self.liq_fee_rate = liq_fee_rate
        self.cash = cfg.initial_capital
        self.positions: dict[str, LevPosition] = {}
        self.pending: dict[str, object] = {}
        self.trades: list[Trade] = []
        self.trades_total = 0  # lifetime count (self.trades is bounded in the state file)
        self.realized_pnl = 0.0
        self.funding_total = 0.0
        self.marks: dict[str, float] = {}
        self.last_processed_ts: dict[str, object] = {}
        self.last_signals: dict = {}
        self.events: deque[str] = deque(maxlen=400)
        self.liquidations = 0
        self._cooldown: dict[str, int] = {}  # bars remaining before re-entry per symbol
        self.state_path = Path(state_path) if state_path else None
        # balance ledger: one record per open/close with the equity snapshot at that instant.
        # In-memory deque feeds the dashboard; the file (if set) is the durable journal.
        self.ledger_path = ledger_path
        self.ledger: deque[dict] = deque(maxlen=200)
        self.lock = threading.RLock()  # guards state for the dashboard's HTTP reader thread

    def warmup_for(self, symbol: str) -> int:
        return max(getattr(self.strategies[symbol], "warmup_bars", 200), 2)

    # --- portfolio math --------------------------------------------------
    def equity(self) -> float:
        # list() snapshot so a concurrent dashboard read can't hit "dict changed size"
        eq = self.cash
        for sym, p in list(self.positions.items()):
            mark = self.marks.get(sym, p.entry_price)
            eq += p.im + p.unrealized(mark)
        return eq

    def unrealized(self) -> float:
        return sum(p.unrealized(self.marks.get(s, p.entry_price)) for s, p in list(self.positions.items()))

    def total_return(self) -> float:
        return self.equity() / self.cfg.initial_capital - 1.0

    def set_mark(self, symbol: str, price: float):
        if price and price > 0:
            self.marks[symbol] = float(price)

    def update_mark(self, symbol: str, price: float):
        with self.lock:
            return self._update_mark(symbol, price)

    def _update_mark(self, symbol: str, price: float):
        """Live mark update WITH real-time risk check (liquidation/stop/target).

        Used between bars in live mode so a leveraged position is closed the moment the
        live price crosses its level, not only at bar close."""
        if not price or price <= 0:
            return
        self.marks[symbol] = float(price)
        p = self.positions.get(symbol)
        if p is None:
            return
        self._update_trailing(p, price, price)
        slip = self.cfg.slippage_bps / 1e4
        reason = fill = None
        if p.side == "LONG":
            trig = max(p.stop_price, p.liq)
            if price <= trig:
                if p.liq >= p.stop_price:
                    reason, fill = "liquidation", p.liq * (1 - slip)
                else:
                    reason, fill = "stop", min(p.stop_price, price) * (1 - slip)
            elif p.target_price is not None and price >= p.target_price:
                reason, fill = "target", price * (1 + slip)
        else:
            trig = min(p.stop_price, p.liq)
            if price >= trig:
                if p.liq <= p.stop_price:
                    reason, fill = "liquidation", p.liq * (1 + slip)
                else:
                    reason, fill = "stop", max(p.stop_price, price) * (1 + slip)
            elif p.target_price is not None and price <= p.target_price:
                reason, fill = "target", price * (1 - slip)
        if reason:
            self._close(symbol, fill, pd.Timestamp.now(tz="UTC"), reason)

    # --- core step (per symbol) -----------------------------------------
    def step(self, symbol: str, df):
        with self.lock:
            return self._step(symbol, df)

    def _step(self, symbol: str, df):
        if df is None or df.empty:
            return
        newest = df.iloc[-1]
        ts = newest["timestamp"]
        if self.last_processed_ts.get(symbol) is not None and ts <= self.last_processed_ts[symbol]:
            self.marks[symbol] = float(newest["close"])  # keep mark fresh between bars
            return
        o, h, l, c = (float(newest["open"]), float(newest["high"]), float(newest["low"]), float(newest["close"]))
        self.marks[symbol] = c
        slip = self.cfg.slippage_bps / 1e4
        fee_rate = self.cfg.fee_rate
        if self._cooldown.get(symbol, 0) > 0:  # anti-whipsaw cooldown after a recent exit
            self._cooldown[symbol] -= 1

        # (1) fill pending from previous bar at this open
        pend = self.pending.get(symbol)
        if pend is not None:
            kind = pend[0]
            if kind == "open" and symbol not in self.positions:
                sig, side = pend[1], pend[2]
                fill = o * (1 + slip) if side == "LONG" else o * (1 - slip)
                # buying power capped so a single position can't hog all margin (keeps
                # capital free to diversify across many coins)
                cap_pct = getattr(self.cfg, "max_position_pct", 1.0)
                max_notional = min(self.cash, self.equity() * cap_pct) * self.leverage
                sized = self.risk.size_position(
                    capital_available=self.equity(),  # risk base = total equity (stable as cash shifts)
                    entry_price=fill,  # size at the actual fill, not the signal bar's close (gap-safe)
                    stop_price=sig.stop_price,
                    min_notional=self.cfg.min_notional,
                    lot_step=self.cfg.lot_step,
                    max_notional=max_notional,
                )
                if not sized.approved:
                    self._log(f"{symbol} entry skip: {sized.reason}")
                elif len(self.positions) >= self.risk.s.max_open_positions:
                    self._log(f"{symbol} entry skip: max_open_positions")
                else:
                    notional = fill * sized.quantity
                    im = notional / self.leverage
                    fee = notional * fee_rate
                    eq = self.equity()
                    cur_notional = sum(pp.entry_price * pp.qty for pp in self.positions.values())
                    max_exp = getattr(self.risk.s, "max_total_exposure", 1e9)
                    if eq > 0 and (cur_notional + notional) / eq > max_exp:
                        self._log(f"{symbol} entry skip: total exposure cap ({max_exp:g}x)")
                    elif im + fee <= self.cash:
                        self.cash -= im + fee
                        liq = futures.liquidation_price(side, fill, sized.quantity, self.leverage, notional, fee_rate=fee_rate + self.liq_fee_rate)
                        self.positions[symbol] = LevPosition(
                            symbol, side, fill, sized.quantity, im, self.leverage,
                            sig.stop_price, sig.target_price, ts, fee, liq,
                            peak=fill, atr_entry=sig.atr, init_stop=sig.stop_price,
                        )
                        lv = f" x{self.leverage:g}" if self.leverage > 1 else ""
                        self._log(f"{symbol} {side} {sized.quantity:.6f} @ {fill:.2f}{lv} stop {sig.stop_price:.2f} liq {liq:.2f}")
                        self._record_ledger("OPEN", symbol, side, fill, sized.quantity, ts)
                    else:
                        self._log(f"{symbol} entry skip: margin {im:.2f} > cash {self.cash:.2f}")
            elif kind == "close" and symbol in self.positions:
                p = self.positions[symbol]
                fill = o * (1 - slip) if p.side == "LONG" else o * (1 + slip)
                self._close(symbol, fill, ts, pend[1])
            self.pending[symbol] = None

        # (2) funding on the open position (perpetual: longs pay shorts when rate>0)
        p = self.positions.get(symbol)
        if p is not None and self.funding_rate:
            f = futures.funding_per_bar(p.entry_price * p.qty, self.funding_rate, self.bar_hours)
            pay = f if p.side == "LONG" else -f
            self.cash -= pay
            p.funding_paid += pay
            self.funding_total += pay

        # (3) intrabar stop / liquidation / target (per side) FIRST, then ratchet the trail
        # for the NEXT bar — updating the trail with this bar's high before checking this
        # bar's low would be optimistic same-bar lookahead.
        p = self.positions.get(symbol)
        if p is not None:
            reason = level = fill = None
            if p.side == "LONG":
                trig = max(p.stop_price, p.liq)
                if l <= trig:
                    if p.liq >= p.stop_price:
                        # liquidation settles AT the liq price (insurance fund eats any gap)
                        reason, fill = "liquidation", p.liq * (1 - slip)
                    else:
                        reason, fill = "stop", min(p.stop_price, o) * (1 - slip)  # market stop can gap
                elif p.target_price is not None and h >= p.target_price:
                    reason, fill = "target", max(p.target_price, o) * (1 + slip)
            else:  # SHORT
                trig = min(p.stop_price, p.liq)
                if h >= trig:
                    if p.liq <= p.stop_price:
                        reason, fill = "liquidation", p.liq * (1 + slip)
                    else:
                        reason, fill = "stop", max(p.stop_price, o) * (1 + slip)
                elif p.target_price is not None and l <= p.target_price:
                    reason, fill = "target", min(p.target_price, o) * (1 - slip)
            if reason:
                self._close(symbol, fill, ts, reason)
            else:
                self._update_trailing(p, h, l)  # ratchet for the NEXT bar (no same-bar lookahead)

        # (4) decision -> stage for next bar
        sig = self.strategies[symbol].generate_signal(df, self._position_state(symbol))
        self.last_signals[symbol] = sig
        flat = symbol not in self.positions and self.pending.get(symbol) is None
        if sig.action == Action.BUY and flat and self._entry_allowed(symbol, sig, "LONG"):
            self.pending[symbol] = ("open", sig, "LONG")
        elif sig.action == Action.SHORT and flat and self._entry_allowed(symbol, sig, "SHORT"):
            self.pending[symbol] = ("open", sig, "SHORT")
        elif sig.action in (Action.SELL, Action.COVER) and symbol in self.positions:
            self.pending[symbol] = ("close", f"signal:{sig.reason}")

        self.last_processed_ts[symbol] = ts
        self.save()

    def _entry_allowed(self, symbol, sig, side: str) -> bool:
        """Durable cost/correlation gates before staging an entry (all inert-by-default-ish)."""
        if self._cooldown.get(symbol, 0) > 0:  # anti-whipsaw cooldown after a recent exit
            return False
        # min-edge cost floor: skip entries whose expected move can't clear round-trip cost.
        # For targeted (mean-reversion) signals use the target distance; for targetless trend
        # entries use only an ATR viability floor so high-R tight-stop winners are NOT filtered.
        rt = 2 * self.cfg.fee_rate + 2 * self.cfg.slippage_bps / 1e4
        if sig.bar_close:
            if sig.target_price is not None:
                edge = abs(sig.target_price - sig.bar_close) / sig.bar_close
            elif sig.atr:
                edge = sig.atr / sig.bar_close
            else:
                edge = float("inf")
            if edge < self.cfg.min_edge_mult * rt:
                self._log(f"{symbol} entry skip: edge<cost ({edge*100:.2f}% < {self.cfg.min_edge_mult*rt*100:.2f}%)")
                return False
        # correlation control: cap simultaneous same-direction positions
        same = sum(1 for p in self.positions.values() if p.side == side)
        if same >= self.cfg.max_same_side:
            self._log(f"{symbol} entry skip: max_same_side ({side})")
            return False
        return True

    def _position_state(self, symbol: str) -> PositionState:
        p = self.positions.get(symbol)
        if p is None:
            return PositionState(state="FLAT")
        return PositionState(
            state=p.side, symbol=symbol, entry_price=p.entry_price, quantity=p.qty,
            stop_price=p.stop_price, target_price=p.target_price, entry_ts=p.entry_ts,
        )

    def _update_trailing(self, p: "LevPosition", hi: float, lo: float):
        """Ratchet the stop toward the high-water mark to protect open profit.

        (1) Break-even: once up `breakeven_r` initial-risks, pull the stop to entry so a
        winner can no longer become a loser. (2) Chandelier: keep the stop
        `trail_atr_mult` ATRs below the peak (above for shorts). The stop only ever moves
        in the profit direction, never loosens."""
        if not getattr(self.cfg, "use_trailing", True) or not p.atr_entry or not p.init_stop:
            return
        risk = abs(p.entry_price - p.init_stop)
        if risk <= 0:
            return
        if p.side == "LONG":
            if hi > p.peak:
                p.peak = hi
            candidate = p.peak - self.cfg.trail_atr_mult * p.atr_entry
            if p.peak >= p.entry_price + self.cfg.breakeven_r * risk:
                candidate = max(candidate, p.entry_price)  # lock break-even
            if candidate > p.stop_price:
                p.stop_price = candidate
        else:  # SHORT
            if lo < p.peak:
                p.peak = lo
            candidate = p.peak + self.cfg.trail_atr_mult * p.atr_entry
            if p.peak <= p.entry_price - self.cfg.breakeven_r * risk:
                candidate = min(candidate, p.entry_price)
            if candidate < p.stop_price:
                p.stop_price = candidate

    def _close(self, symbol: str, fill: float, ts, reason: str):
        p = self.positions.pop(symbol)
        exit_fee = fill * p.qty * self.cfg.fee_rate
        liq_fee = (fill * p.qty * self.liq_fee_rate) if reason == "liquidation" else 0.0
        price_pnl = futures.unrealized_pnl(p.side, p.entry_price, p.qty, fill)
        gross_return = p.im + price_pnl - exit_fee - liq_fee
        if self.leverage > 1 or reason == "liquidation":
            # isolated margin: a leveraged position can never lose more than its posted
            # margin, regardless of how the exit is labelled (a gap through a trailed stop
            # that sits above the liq price must not drain the shared cash pool)
            gross_return = max(0.0, gross_return)
        self.cash += gross_return
        pnl = gross_return - p.im - p.entry_fee - p.funding_paid
        self.realized_pnl += pnl
        if reason == "liquidation":
            self.liquidations += 1
        self.trades.append(Trade(
            entry_time=p.entry_ts, entry_price=p.entry_price, exit_time=ts, exit_price=fill,
            qty=p.qty, fees=p.entry_fee + exit_fee + liq_fee, pnl=pnl,
            pnl_pct=(pnl / p.im) if p.im else 0.0, bars_held=0, exit_reason=reason,
        ))
        self.trades_total += 1
        tag = {"liquidation": "LIQUIDATED", "stop": "STOP", "target": "TARGET"}.get(reason, "CLOSE")
        self._log(f"{symbol} {p.side} {tag} {p.qty:.6f} @ {fill:.2f} pnl {pnl:+.2f} ({reason})")
        self.pending[symbol] = None
        self._cooldown[symbol] = int(getattr(self.cfg, "cooldown_bars", 0))  # wait before re-entry
        self._record_ledger("CLOSE", symbol, p.side, fill, p.qty, ts, pnl=pnl, reason=reason)

    def _record_ledger(self, event: str, symbol: str, side: str, price: float, qty: float,
                       ts, pnl: float | None = None, reason: str | None = None):
        """Snapshot the balance at the instant a position opens/closes -> deque + journal file.

        Called AFTER the cash/position change so equity() reflects the post-event balance."""
        rec = {
            "ts": str(ts), "event": event, "symbol": symbol, "side": side,
            "price": round(float(price), 6), "qty": round(float(qty), 8),
            "equity": round(self.equity(), 2), "cash": round(self.cash, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized(), 2),
            "source": "serve",
        }
        if pnl is not None:
            rec["pnl"] = round(float(pnl), 2)
        if reason is not None:
            rec["reason"] = reason
        self.ledger.append(rec)
        append_ledger(self.ledger_path, rec)

    def _log(self, msg: str):
        self.events.append(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S  ") + msg)

    # --- persistence -----------------------------------------------------
    def save(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        positions = {}
        for sym, p in self.positions.items():
            d = asdict(p)
            d["entry_ts"] = pd.Timestamp(p.entry_ts).isoformat() if p.entry_ts is not None else None
            positions[sym] = d
        data = {
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "leverage": self.leverage,
            "initial_capital": self.cfg.initial_capital,
            "positions": positions,
            "last_processed_ts": {s: (pd.Timestamp(t).isoformat() if t is not None else None) for s, t in self.last_processed_ts.items()},
            "num_trades": max(self.trades_total, len(self.trades)),
            "trades_total": max(self.trades_total, len(self.trades)),
            # bounded trade history so it survives once-per-run jobs (GitHub Actions)
            "trades": [self._trade_dict(t) for t in self.trades[-500:]],
            "liquidations": self.liquidations,
            "funding_total": self.funding_total,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    @staticmethod
    def _trade_dict(t: Trade) -> dict:
        d = asdict(t)
        for k in ("entry_time", "exit_time"):
            d[k] = pd.Timestamp(d[k]).isoformat() if d[k] is not None else None
        return d

    def load(self) -> bool:
        """Restore cash/positions/trades from disk (resume across once-per-run jobs).

        A corrupt state file raises (after saving a .corrupt-* backup) instead of
        silently restarting at initial capital — losing the account history must be
        a loud, operator-visible failure, never an accidental reset."""
        if not self.state_path or not self.state_path.exists():
            return False
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as e:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = self.state_path.with_name(self.state_path.name + f".corrupt-{stamp}")
            shutil.copy2(self.state_path, backup)
            raise RuntimeError(
                f"corrupt state file {self.state_path} (backed up to {backup}); "
                "refusing to reset to initial capital — inspect or delete the file to start fresh"
            ) from e
        import dataclasses
        self.cash = float(data.get("cash", self.cash))
        self.realized_pnl = float(data.get("realized_pnl", 0.0))
        self.liquidations = int(data.get("liquidations", 0))
        self.funding_total = float(data.get("funding_total", 0.0))
        fields = {f.name for f in dataclasses.fields(LevPosition)}
        self.positions = {}
        for sym, d in data.get("positions", {}).items():
            d = {k: v for k, v in dict(d).items() if k in fields}  # tolerate schema drift
            ts = d.get("entry_ts")
            d["entry_ts"] = pd.Timestamp(ts) if ts else None
            self.positions[sym] = LevPosition(**d)
        self.last_processed_ts = {s: (pd.Timestamp(t) if t else None)
                                  for s, t in data.get("last_processed_ts", {}).items()}
        tfields = {f.name for f in dataclasses.fields(Trade)}
        self.trades = []
        for d in data.get("trades", []):  # tolerate pre-history state files (no "trades" key)
            d = {k: v for k, v in dict(d).items() if k in tfields}
            for k in ("entry_time", "exit_time"):
                d[k] = pd.Timestamp(d[k]) if d.get(k) else None
            self.trades.append(Trade(**d))
        self.trades_total = int(data.get("trades_total", data.get("num_trades", len(self.trades))))
        return True
