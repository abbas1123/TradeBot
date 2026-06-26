"""Order-approval gate + broker safety (dry-run, no network)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from config.settings import Mode
from src.execution.broker import Broker
from src.risk.manager import RiskManager
from src.risk.types import ApprovedOrder, Side


def _settings(**kw):
    base = dict(
        mode=Mode.BACKTEST, exchange="binance", binance_api_key="", binance_api_secret="",
        binance_testnet=True, taker_fee_pct=0.1, initial_capital=1000.0, live_capital_cap=1e12,
        risk_per_trade_pct=1.0, max_open_positions=5, max_daily_loss_pct=5.0, max_consecutive_errors=5,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_approve_entry_mints_single_use_token():
    rm = RiskManager(_settings())
    appr = rm.approve_entry("BTC/USDT", 100, 92, 1000, {}, min_notional=5)
    assert isinstance(appr, ApprovedOrder)
    assert rm.consume_token(appr._token) is True
    assert rm.consume_token(appr._token) is False  # single use — replay rejected


def test_approve_entry_rejected_when_killed():
    rm = RiskManager(_settings())
    rm.set_manual_kill(True)
    appr = rm.approve_entry("BTC/USDT", 100, 92, 1000, {}, min_notional=5)
    assert not isinstance(appr, ApprovedOrder)  # RejectedOrder


def test_approve_exit_always_allowed_even_when_killed():
    rm = RiskManager(_settings())
    rm.set_manual_kill(True)
    appr = rm.approve_exit("BTC/USDT", 0.5)
    assert isinstance(appr, ApprovedOrder) and appr.reduce_only


def test_broker_dry_run_has_no_exchange_and_fills():
    rm = RiskManager(_settings())
    br = Broker(_settings(), rm)
    assert br.exchange is None  # never builds a network client in dry-run
    appr = rm.approve_entry("BTC/USDT", 100, 92, 1000, {}, min_notional=5)
    fill = br.place_order(appr, 100.0)
    assert fill.status == "filled" and fill.filled_qty > 0


def test_broker_rejects_non_approved_object():
    rm = RiskManager(_settings())
    br = Broker(_settings(), rm)
    with pytest.raises(TypeError):
        br.place_order({"symbol": "BTC/USDT"}, 100.0)


def test_broker_rejects_forged_token():
    rm = RiskManager(_settings())
    br = Broker(_settings(), rm)
    forged = ApprovedOrder("BTC/USDT", Side.BUY, 1.0, False, "x", _token="forged")
    with pytest.raises(PermissionError):
        br.place_order(forged, 100.0)


def test_broker_skips_below_min_notional():
    rm = RiskManager(_settings())
    br = Broker(_settings(), rm)
    appr = rm.approve_exit("BTC/USDT", 0.0001)  # tiny -> notional 0.0001 << 5
    fill = br.place_order(appr, 1.0)
    assert fill.status.startswith("skipped")
