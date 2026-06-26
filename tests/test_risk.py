"""Risk manager tests: sizing caps, min-notional skip, limits, kill switch."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.risk.manager import RiskManager


def test_sizing_basic(risk_settings):
    rm = RiskManager(risk_settings)
    # capital 400, risk 1% -> $4 risk; entry 100, stop 92 -> 8 wide -> qty 0.5
    r = rm.size_position(400, 100, 92, min_notional=5)
    assert r.approved
    assert r.quantity == pytest.approx(0.5)
    assert r.notional == pytest.approx(50.0)


def test_sizing_capped_by_capital(risk_settings):
    rm = RiskManager(risk_settings)
    # tight 0.5% stop would size huge; capped to available capital (400)
    r = rm.size_position(400, 100, 99.5, min_notional=5)
    assert r.approved
    assert r.quantity == pytest.approx(4.0)  # 400 / 100
    assert r.notional == pytest.approx(400.0)


def test_min_notional_skips(risk_settings):
    rm = RiskManager(risk_settings)
    r = rm.size_position(400, 100, 92, min_notional=100)  # notional 50 < 100
    assert not r.approved
    assert r.reason == "below_min_notional"


def test_lot_step_rounds_down(risk_settings):
    rm = RiskManager(risk_settings)
    # entry 100, stop 84 -> 16 wide -> qty 0.25; floor to 0.1 step -> 0.2
    r = rm.size_position(400, 100, 84, min_notional=5, lot_step=0.1)
    assert r.approved
    assert r.quantity == pytest.approx(0.2)


def test_live_capital_cap_supreme():
    s = SimpleNamespace(
        live_capital_cap=100.0,
        risk_per_trade_pct=1.0,
        max_open_positions=2,
        max_daily_loss_pct=5.0,
        max_consecutive_errors=5,
    )
    rm = RiskManager(s)
    # balance 1000 but cap 100; tight stop -> position_value capped at 100
    r = rm.size_position(1000, 100, 99.5, min_notional=5)
    assert r.approved
    assert r.notional <= 100.0 + 1e-9
    assert r.quantity == pytest.approx(1.0)


def test_stop_too_tight(risk_settings):
    rm = RiskManager(risk_settings)
    r = rm.size_position(400, 100, 100, min_notional=5)
    assert not r.approved
    assert r.reason == "stop_too_tight"


def test_capital_below_min_notional(risk_settings):
    rm = RiskManager(risk_settings)
    r = rm.size_position(3, 100, 90, min_notional=5)
    assert not r.approved
    assert r.reason == "capital_below_min_notional"


def test_max_open_positions(risk_settings):
    rm = RiskManager(risk_settings)
    ok, _ = rm.can_open("BTC/USDT", {"A": 1})
    assert ok
    blocked, reason = rm.can_open("BTC/USDT", {"A": 1, "B": 1})
    assert not blocked
    assert reason == "max_open_positions"


def test_already_in_position(risk_settings):
    rm = RiskManager(risk_settings)
    blocked, reason = rm.can_open("BTC/USDT", {"BTC/USDT": 1})
    assert not blocked
    assert reason == "already_in_position"


def test_daily_loss_trips_kill(risk_settings):
    rm = RiskManager(risk_settings)
    rm.register_pnl(-1000.0, capital_reference=100.0)  # limit is -5
    assert rm.is_killed
    ok, reason = rm.can_open("BTC/USDT", {})
    assert not ok


def test_consecutive_errors_trip_kill(risk_settings):
    rm = RiskManager(risk_settings)
    for _ in range(5):
        rm.record_error()
    assert rm.is_killed


def test_record_success_resets_error_counter(risk_settings):
    rm = RiskManager(risk_settings)
    rm.record_error()
    rm.record_error()
    rm.record_success()
    assert rm.consecutive_errors == 0
    assert not rm.is_killed


def test_manual_kill_blocks_entry(risk_settings):
    rm = RiskManager(risk_settings)
    rm.set_manual_kill(True)
    assert rm.is_killed
    ok, _ = rm.can_open("BTC/USDT", {})
    assert not ok
