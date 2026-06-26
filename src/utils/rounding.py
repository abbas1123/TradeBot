"""Decimal-safe rounding for exchange quantity/price filters.

Float math (0.1*3 == 0.30000000000000004) causes Binance LOT_SIZE (-1013) rejections,
so we convert through Decimal(str(x)). Quantity is ALWAYS floored to the lot step
(rounding up would overspend and breach the risk size).
"""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal


def floor_to_step(quantity: float, step: float) -> float:
    """Floor quantity to the nearest multiple of step. step<=0 returns quantity as-is."""
    if step is None or step <= 0:
        return float(quantity)
    q = Decimal(str(quantity))
    s = Decimal(str(step))
    return float((q // s) * s)


def round_to_tick(price: float, tick: float) -> float:
    """Round price to the nearest tick (half-up). tick<=0 returns price as-is."""
    if tick is None or tick <= 0:
        return float(price)
    p = Decimal(str(price))
    t = Decimal(str(tick))
    return float((p / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * t)
