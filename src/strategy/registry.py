"""Strategy registry: name -> class, so config can select a strategy by string."""
from __future__ import annotations

from .base import Strategy
from .confluence import ConfluenceStrategy
from .donchian import DonchianTrendStrategy
from .donchian_futures import DonchianFuturesStrategy
from .mean_reversion import MeanReversionStrategy
from .regime import RegimeSwitchStrategy

_REGISTRY: dict[str, type[Strategy]] = {
    "donchian": DonchianTrendStrategy,
    "confluence": ConfluenceStrategy,
    "donchian_futures": DonchianFuturesStrategy,
    "futures": DonchianFuturesStrategy,
    "mean_reversion": MeanReversionStrategy,
    "regime": RegimeSwitchStrategy,
}


def get_strategy(name: str, params) -> Strategy:
    key = name.strip().lower()
    if key not in _REGISTRY:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {', '.join(sorted(_REGISTRY))}"
        )
    return _REGISTRY[key](params)


def available_strategies() -> list[str]:
    return sorted(_REGISTRY)
