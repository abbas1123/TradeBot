"""Shared level-exit checker used by BOTH the backtester and the live runner.

Signal-driven exits (RSI>exit, close<EMA200, close<Donchian-low) come out of
generate_signal(). Level-driven exits (a price *touch* of the hard stop or take-profit)
are checked here against a bar's intrabar high/low. Keeping this in one place is what
prevents backtest and live execution from diverging.
"""
from __future__ import annotations

from .base import PositionState


def check_level_exit(
    position: PositionState | None, bar_high: float, bar_low: float
) -> tuple[str, float] | None:
    """Return (reason, exit_price) if the stop or target is touched this bar, else None.

    Stop is checked first (the adverse fill) so that if a single bar both pierces the
    stop and reaches the target, the conservative stop outcome wins.
    """
    if position is None or position.is_flat:
        return None
    if position.stop_price is not None and bar_low <= position.stop_price:
        return ("stop", position.stop_price)
    if position.target_price is not None and bar_high >= position.target_price:
        return ("target", position.target_price)
    return None
