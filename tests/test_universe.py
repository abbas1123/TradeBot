"""Universe selection: leveraged-token filter must not eat real coins (JUP, SUPER)."""
from __future__ import annotations

import pytest

from src.data.universe import _looks_leveraged, top_symbols


@pytest.mark.parametrize(
    "base,expected",
    [
        ("JUP", False),      # regression: "UP" in "JUP" used to exclude Jupiter
        ("SUPER", False),    # regression: "UP" in "SUPER"
        ("SYRUP", False),    # ends with UP but is a real coin (allowlist)
        ("PUMP", False),
        ("BTC", False),
        ("BTCUP", True),
        ("ETHBULL", True),
        ("ADADOWN", True),
        ("XRP3L", True),
        ("LTC5S", True),
    ],
)
def test_looks_leveraged(base, expected):
    assert _looks_leveraged(base) is expected


class _FakeEx:
    def __init__(self):
        self.markets = {
            s: {"spot": True, "active": True}
            for s in ("BTC/USDT", "ETH/USDT", "JUP/USDT", "BTCUP/USDT", "USDC/USDT", "SOL/BTC")
        }
        self.symbols = list(self.markets)

    def load_markets(self):
        return self.markets

    def fetch_tickers(self):
        return {
            "BTC/USDT": {"quoteVolume": 100.0},
            "ETH/USDT": {"quoteVolume": 80.0},
            "JUP/USDT": {"quoteVolume": 50.0},
            "BTCUP/USDT": {"quoteVolume": 999.0},  # leveraged: must be excluded despite volume
            "USDC/USDT": {"quoteVolume": 500.0},   # stable base: excluded
            "SOL/BTC": {"quoteVolume": 1000.0},    # wrong quote: excluded
        }


def test_top_symbols_filters_and_orders_by_volume():
    got = top_symbols(_FakeEx(), n=5)
    assert got == ["BTC/USDT", "ETH/USDT", "JUP/USDT"]
