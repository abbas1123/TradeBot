"""Settings parsing regression tests (env-sourced list field must not be JSON-decoded)."""
from __future__ import annotations

from config.settings import Settings


def test_pairs_from_env_comma_string(monkeypatch):
    # regression: PAIRS=BTC/USDT used to crash (pydantic tried json.loads on the list field)
    monkeypatch.setenv("PAIRS", "BTC/USDT,ETH/USDT")
    s = Settings(_env_file=None)
    assert s.pairs == ["BTC/USDT", "ETH/USDT"]


def test_pairs_from_env_single(monkeypatch):
    monkeypatch.setenv("PAIRS", "btc/usdt")
    s = Settings(_env_file=None)
    assert s.pairs == ["BTC/USDT"]  # upper-cased, single element


def test_pairs_default(monkeypatch):
    monkeypatch.delenv("PAIRS", raising=False)
    s = Settings(_env_file=None)
    assert s.pairs == ["BTC/USDT"]
