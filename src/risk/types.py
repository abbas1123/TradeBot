"""Value objects for the risk layer."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class SizingResult:
    """Outcome of position sizing. approved=False means SKIP the trade (with a reason)."""

    quantity: float  # base-currency quantity (floored to lot step when one is given)
    notional: float  # quantity * entry_price (quote currency)
    approved: bool
    reason: str


@dataclass(frozen=True)
class OrderRequest:
    """What the runner proposes to the RiskManager."""

    symbol: str
    side: Side
    entry_price: float  # reference price (last close / ticker)
    stop_price: float  # strategy stop, enables risk sizing (ignored for exits)
    is_entry: bool  # True = opening; False = exit/close


@dataclass(frozen=True)
class ApprovedOrder:
    """ONLY RiskManager constructs this. The broker refuses anything else, and the single-
    use _token (minted by the live RiskManager) makes a forged approval unusable."""

    symbol: str
    side: Side
    quantity: float  # risk-sized base qty (broker still floor-rounds to lot step)
    reduce_only: bool
    reason: str
    _token: str = ""


@dataclass(frozen=True)
class RejectedOrder:
    symbol: str
    reason: str


@dataclass(frozen=True)
class Fill:
    """Normalized order result from the broker (dry-run or real)."""

    symbol: str
    side: Side
    filled_qty: float
    avg_price: float
    fee: float  # quote currency
    cost: float  # filled_qty * avg_price
    status: str  # "filled" | "partial" | "skipped" | "rejected"
    raw: dict | None = None

    @classmethod
    def skipped(cls, symbol: str, side: Side, reason: str) -> "Fill":
        return cls(symbol, side, 0.0, 0.0, 0.0, 0.0, f"skipped:{reason}")
