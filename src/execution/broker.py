"""Broker — thin, SAFE CCXT wrapper gated by MODE.

Three behaviours: dry-run (backtest/no-keys: simulate fills, never touches the network),
testnet (Binance Spot Testnet), live (real Binance). A single boolean
`_can_place_real_orders` gates every network write. Orders are accepted ONLY as an
ApprovedOrder minted by the RiskManager, and its single-use token is consumed here — so
trading cannot bypass risk. Quantities floor to the exchange lot step; orders below
MIN_NOTIONAL are skipped (never forced).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..risk.types import ApprovedOrder, Fill, Side
from ..utils.rounding import floor_to_step, round_to_tick


@dataclass(frozen=True)
class SymbolFilters:
    step_size: float
    min_qty: float
    min_notional: float
    price_tick: float


class Broker:
    def __init__(self, settings, risk):
        from config.settings import Mode

        self.s = settings
        self.risk = risk
        self.fee_rate = settings.taker_fee_pct / 100.0
        self._can_place_real_orders = settings.mode in (Mode.PAPER, Mode.LIVE)
        self.exchange = self._build_exchange() if self._can_place_real_orders else None
        self._markets: dict = {}

    def _build_exchange(self):
        import ccxt

        klass = getattr(ccxt, self.s.exchange)
        ex = klass(
            {
                "apiKey": self.s.binance_api_key,
                "secret": self.s.binance_api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot", "adjustForTimeDifference": True},
            }
        )
        if self.s.binance_testnet:
            ex.set_sandbox_mode(True)
        ex.load_markets()
        return ex

    # --- account / data --------------------------------------------------
    def free_quote(self, quote: str = "USDT") -> float:
        if not self.exchange:
            return self.s.initial_capital  # dry-run notional balance
        bal = self._with_backoff(self.exchange.fetch_balance)
        try:
            return float(bal.get(quote, {}).get("free", 0.0) or 0.0)
        except (AttributeError, TypeError):
            return 0.0

    def ref_price(self, symbol: str) -> float:
        if not self.exchange:
            return 0.0
        t = self._with_backoff(self.exchange.fetch_ticker, symbol)
        return float(t.get("last") or t.get("close") or 0.0)

    def get_filters(self, symbol: str) -> SymbolFilters:
        if not self.exchange:
            return SymbolFilters(1e-8, 0.0, 5.0, 1e-2)
        m = self._markets.get(symbol) or self.exchange.market(symbol)
        self._markets[symbol] = m
        limits, info = m.get("limits", {}), m.get("info", {})
        step = _raw_filter(info, "LOT_SIZE", "stepSize") or (limits.get("amount", {}) or {}).get("min") or 1e-8
        min_qty = (limits.get("amount", {}) or {}).get("min") or 0.0
        min_notional = (
            _raw_filter(info, "NOTIONAL", "minNotional")
            or _raw_filter(info, "MIN_NOTIONAL", "minNotional")
            or (limits.get("cost", {}) or {}).get("min")
            or 5.0
        )
        tick = _raw_filter(info, "PRICE_FILTER", "tickSize") or 1e-2
        return SymbolFilters(float(step), float(min_qty), float(min_notional), float(tick))

    # --- the one method that places orders -------------------------------
    def place_order(self, order: ApprovedOrder, ref_price: float) -> Fill:
        if not isinstance(order, ApprovedOrder):
            raise TypeError("Broker only accepts ApprovedOrder minted by RiskManager")
        if not self.risk.consume_token(order._token):
            raise PermissionError("Order token invalid or already used — refusing to trade")

        f = self.get_filters(order.symbol)
        qty = floor_to_step(order.quantity, f.step_size)
        if qty <= 0 or qty < f.min_qty:
            return Fill.skipped(order.symbol, order.side, "below_min_qty_after_rounding")
        if ref_price > 0 and qty * ref_price < f.min_notional:
            return Fill.skipped(order.symbol, order.side, "below_min_notional_after_rounding")

        if not self._can_place_real_orders:  # dry-run / backtest: simulate
            cost = qty * ref_price
            return Fill(order.symbol, order.side, qty, ref_price, cost * self.fee_rate, cost, "filled")

        params = {"reduceOnly": True} if order.reduce_only else {}
        raw = self._with_backoff(
            self.exchange.create_order, order.symbol, "market", order.side.value, qty, None, params
        )
        return self._normalize_fill(raw, order)

    def _normalize_fill(self, raw: dict, order: ApprovedOrder) -> Fill:
        filled = float(raw.get("filled") or 0.0)
        avg = float(raw.get("average") or raw.get("price") or 0.0)
        fees = raw.get("fees") or ([raw["fee"]] if raw.get("fee") else [])
        fee_quote = 0.0
        for fe in fees:
            try:
                fee_quote += float(fe.get("cost") or 0.0)
            except (AttributeError, TypeError):
                pass
        status = "filled" if (filled and raw.get("status") in ("closed", "filled")) else (raw.get("status") or "partial")
        return Fill(order.symbol, order.side, filled, avg, fee_quote, filled * avg, status, raw)

    def _with_backoff(self, fn, *args, max_attempts: int = 5, base: float = 1.0):
        import ccxt

        delay = base
        for attempt in range(max_attempts):
            try:
                return fn(*args)
            except ccxt.DDoSProtection:
                sleep = max(delay, 5.0)
            except ccxt.RateLimitExceeded:
                sleep = delay
            except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable):
                if attempt == max_attempts - 1:
                    raise
                sleep = delay
            except (ccxt.AuthenticationError, ccxt.InsufficientFunds, ccxt.BadSymbol):
                raise  # do not retry logic errors
            time.sleep(min(sleep, 60.0))
            delay = min(delay * 2, 60.0)
        raise RuntimeError("broker retries exhausted")


def _raw_filter(info: dict, ftype: str, key: str):
    for f in info.get("filters", []) or []:
        if f.get("filterType") == ftype and f.get(key) is not None:
            return float(f[key])
    return None
