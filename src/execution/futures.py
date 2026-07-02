"""Perpetual-futures margin math (Binance USDⓈ-M style), pure & unit-tested.

Implements tiered maintenance margin, isolated-margin liquidation price, ROE and margin
ratio. These are the mechanics a real exchange uses; kept as pure functions so they can
be checked against hand-computed values.

Simplifications vs a real exchange: single isolated position per symbol, no insurance
fund / ADL / partial liquidation, mark price == last price.
"""
from __future__ import annotations

# (max_notional_usdt, maintenance_margin_rate, maintenance_amount) — BTCUSDT-like brackets
BRACKETS = [
    (50_000.0, 0.004, 0.0),
    (500_000.0, 0.005, 50.0),
    (1_000_000.0, 0.010, 2_550.0),
    (5_000_000.0, 0.025, 17_550.0),
    (20_000_000.0, 0.05, 142_550.0),
    (float("inf"), 0.10, 1_142_550.0),
]


def maint_bracket(notional: float) -> tuple[float, float]:
    """Return (maintenance_margin_rate, maintenance_amount) for a notional size."""
    notional = abs(notional)
    for cap, mmr, amt in BRACKETS:
        if notional <= cap:
            return mmr, amt
    return BRACKETS[-1][1], BRACKETS[-1][2]


def initial_margin(notional: float, leverage: float) -> float:
    return abs(notional) / leverage


def liquidation_price(side: str, entry: float, qty: float, leverage: float, notional: float | None = None, fee_rate: float = 0.0) -> float:
    """Isolated-margin liquidation price (closed form, Binance-style).

    Liquidate where position equity == maintenance margin (evaluated AT the liq price)
    plus the closing fees, so the maintenance buffer always covers the clearance cost:
        LONG:  im + (liq-entry)*qty = liq*qty*mmr - amt + liq*qty*fee_rate
        SHORT: im + (entry-liq)*qty = liq*qty*mmr - amt + liq*qty*fee_rate
    `fee_rate` is the round-trip closing rate (taker exit fee + liquidation fee); pass 0
    for the raw maintenance-only price.
    """
    notional = notional if notional is not None else entry * qty
    mmr, amt = maint_bracket(notional)
    im = initial_margin(notional, leverage)
    if side == "LONG":
        denom = qty * (1.0 - mmr - fee_rate)
        return (entry * qty - im - amt) / denom if denom > 0 else 0.0
    denom = qty * (1.0 + mmr + fee_rate)
    return (entry * qty + im + amt) / denom


def unrealized_pnl(side: str, entry: float, qty: float, mark: float) -> float:
    return (mark - entry) * qty if side == "LONG" else (entry - mark) * qty


def roe(side: str, entry: float, qty: float, mark: float, im: float) -> float:
    """Return on equity (on the locked initial margin) — the headline % traders watch."""
    return unrealized_pnl(side, entry, qty, mark) / im if im else 0.0


def margin_ratio(side: str, entry: float, qty: float, mark: float, im: float, notional: float | None = None) -> float:
    """maintenance_margin / margin_balance. Liquidation triggers as this approaches 1.

    Wiped-out equity (<= 0) returns inf — strictly past liquidation, not "exactly at it".
    Callers that serialize this to JSON must map non-finite values themselves."""
    notional = notional if notional is not None else entry * qty
    mmr, amt = maint_bracket(notional)
    mm = notional * mmr - amt
    equity = im + unrealized_pnl(side, entry, qty, mark)
    return mm / equity if equity > 0 else float("inf")


def funding_per_bar(notional: float, funding_rate_8h: float, bar_hours: float) -> float:
    """Funding paid (long) / received (short) over one bar. Binance funds every 8h."""
    return notional * funding_rate_8h * (bar_hours / 8.0)
