"""RiskManager — the central safety component.

It owns position sizing (risk-based), the hard limits (max open positions, max daily
loss), and the kill switch (manual + auto on consecutive errors or the daily-loss
limit). In Phase 3 the broker will route every order through approve_order() so trading
cannot bypass this; for Phase 2 the backtester uses size_position() / can_open() and the
daily-loss + kill-switch logic, all of which are pure and unit-tested.
"""
from __future__ import annotations

import secrets

from .types import ApprovedOrder, RejectedOrder, Side, SizingResult

MIN_STOP_PCT = 0.001  # 0.1% floor on stop distance to avoid div-by-zero / absurd size


class RiskManager:
    def __init__(self, settings):
        self.s = settings
        # mutable risk state (synced with the persistent StateStore in Phase 3)
        self.consecutive_errors: int = 0
        self.realized_daily_pnl: float = 0.0
        self.manual_kill: bool = False
        self.auto_kill: bool = False
        self.kill_reason: str = ""
        self._valid_tokens: set[str] = set()  # single-use approval tokens for the broker

    # --- position sizing -------------------------------------------------
    def size_position(
        self,
        capital_available: float,
        entry_price: float,
        stop_price: float,
        min_notional: float = 0.0,
        lot_step: float | None = None,
        max_notional: float | None = None,
    ) -> SizingResult:
        """Risk-based sizing. Returns approved=False (SKIP) rather than forcing a trade
        that violates a constraint.

        Risk per trade is sized off `capital_available` (the equity/cash at risk). The
        resulting NOTIONAL is capped by `max_notional` when given (e.g. buying power =
        cash * leverage) — so leverage raises position capacity WITHOUT inflating the
        per-trade risk. Without `max_notional`, notional is capped by capital (spot)."""
        if entry_price <= 0:
            return SizingResult(0.0, 0.0, False, "bad_entry_price")

        capital = min(capital_available, self.s.live_capital_cap)  # risk base
        if capital <= 0:
            return SizingResult(0.0, 0.0, False, "no_capital")
        # notional ceiling: buying power if leveraged, else capital; never above the cap
        notional_cap = min(max_notional if max_notional is not None else capital, self.s.live_capital_cap)
        if min_notional and notional_cap < min_notional:
            return SizingResult(0.0, 0.0, False, "capital_below_min_notional")

        stop_distance_abs = abs(entry_price - stop_price)
        stop_distance_pct = stop_distance_abs / entry_price
        if stop_distance_pct < MIN_STOP_PCT:
            return SizingResult(0.0, 0.0, False, "stop_too_tight")

        risk_amount = capital * (self.s.risk_per_trade_pct / 100.0)
        quantity = risk_amount / stop_distance_abs

        # cap notional by buying power (does not change risk_amount)
        position_value = min(quantity * entry_price, notional_cap)
        quantity = position_value / entry_price

        # floor to the exchange lot step (rounding down can drop below min notional)
        if lot_step:
            from ..utils.rounding import floor_to_step

            quantity = floor_to_step(quantity, lot_step)

        notional = quantity * entry_price
        if quantity <= 0:
            return SizingResult(0.0, 0.0, False, "below_min_qty_after_rounding")
        if min_notional and notional < min_notional:
            return SizingResult(quantity, notional, False, "below_min_notional")

        return SizingResult(quantity, notional, True, "ok")

    # --- entry gating ----------------------------------------------------
    def can_open(self, symbol: str, open_positions: dict) -> tuple[bool, str]:
        """Whether a NEW entry on `symbol` is allowed right now."""
        if self.is_killed:
            return False, f"kill_switch_active:{self.kill_reason}"
        if self.daily_loss_exceeded(self._capital_for_loss_check()):
            return False, "daily_loss_halt"
        if symbol in open_positions:
            return False, "already_in_position"
        if len(open_positions) >= self.s.max_open_positions:
            return False, "max_open_positions"
        return True, "ok"

    # --- order approval (the unbypassable gate to the broker) ------------
    def approve_entry(
        self,
        symbol: str,
        entry_price: float,
        stop_price: float,
        capital_available: float,
        open_positions: dict,
        min_notional: float = 0.0,
        lot_step: float | None = None,
    ) -> ApprovedOrder | RejectedOrder:
        ok, why = self.can_open(symbol, open_positions)
        if not ok:
            return RejectedOrder(symbol, why)
        sized = self.size_position(capital_available, entry_price, stop_price, min_notional, lot_step)
        if not sized.approved:
            return RejectedOrder(symbol, sized.reason)
        return self._mint(symbol, Side.BUY, sized.quantity, reduce_only=False, reason="entry")

    def approve_exit(self, symbol: str, quantity: float) -> ApprovedOrder:
        # exits are ALWAYS allowed (even when killed) so risk can always be reduced
        return self._mint(symbol, Side.SELL, quantity, reduce_only=True, reason="exit")

    def _mint(self, symbol, side, quantity, reduce_only, reason) -> ApprovedOrder:
        token = secrets.token_hex(16)
        self._valid_tokens.add(token)
        return ApprovedOrder(symbol, side, quantity, reduce_only, reason, _token=token)

    def consume_token(self, token: str) -> bool:
        """Broker calls this; a token is valid once. Returns False for forged/replayed."""
        if token in self._valid_tokens:
            self._valid_tokens.discard(token)
            return True
        return False

    # --- daily loss / pnl ------------------------------------------------
    def register_pnl(self, pnl: float, capital_reference: float) -> None:
        """Record realized PnL for the day; auto-trip the kill switch if the daily-loss
        limit is breached."""
        self.realized_daily_pnl += pnl
        if self.daily_loss_exceeded(capital_reference):
            self.trip_kill("daily_loss_limit")

    def daily_loss_exceeded(self, capital_reference: float) -> bool:
        if capital_reference <= 0:
            return False
        limit = -(capital_reference * self.s.max_daily_loss_pct / 100.0)
        return self.realized_daily_pnl <= limit

    def reset_day(self) -> None:
        self.realized_daily_pnl = 0.0

    def _capital_for_loss_check(self) -> float:
        # conservative reference; the runner passes a live balance in Phase 3
        return self.s.live_capital_cap

    # --- error tracking --------------------------------------------------
    def record_error(self) -> None:
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.s.max_consecutive_errors:
            self.trip_kill(f"consecutive_errors>={self.s.max_consecutive_errors}")

    def record_success(self) -> None:
        self.consecutive_errors = 0

    # --- kill switch -----------------------------------------------------
    def trip_kill(self, reason: str) -> None:
        self.auto_kill = True
        self.kill_reason = reason

    def set_manual_kill(self, on: bool = True) -> None:
        self.manual_kill = on
        if on:
            self.kill_reason = "manual"

    def reset_kill(self) -> None:
        self.manual_kill = False
        self.auto_kill = False
        self.kill_reason = ""

    @property
    def is_killed(self) -> bool:
        return self.manual_kill or self.auto_kill
