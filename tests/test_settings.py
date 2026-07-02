"""Settings parsing regression tests (env-sourced list field must not be JSON-decoded)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

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


def test_rsi_buy_must_be_below_rsi_exit(monkeypatch):
    monkeypatch.setenv("RSI_BUY", "70")
    monkeypatch.setenv("RSI_EXIT", "30")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_donchian_exit_must_not_exceed_entry(monkeypatch):
    monkeypatch.setenv("DONCHIAN_ENTRY", "20")
    monkeypatch.setenv("DONCHIAN_EXIT", "30")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
