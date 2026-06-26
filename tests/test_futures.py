"""Futures margin math tests (hand-computed)."""
from __future__ import annotations

import pytest

from src.execution import futures


def test_maint_bracket_tiers():
    assert futures.maint_bracket(100) == (0.004, 0.0)
    assert futures.maint_bracket(100_000) == (0.005, 50.0)


def test_liquidation_long():
    # closed form: (entry*qty - im - amt)/(qty*(1-mmr)) = (100-10)/0.996 = 90.3614
    liq = futures.liquidation_price("LONG", 100, 1, 10, 100)
    assert liq == pytest.approx(90.3614, abs=1e-3)


def test_liquidation_short():
    # (entry*qty + im)/(qty*(1+mmr)) = 110/1.004 = 109.5618
    liq = futures.liquidation_price("SHORT", 100, 1, 10, 100)
    assert liq == pytest.approx(109.5618, abs=1e-3)


def test_liquidation_fee_buffer_fires_earlier():
    # baking in closing fees moves the liq price closer to entry (fires earlier)
    no_fee = futures.liquidation_price("LONG", 100, 1, 10, 100, fee_rate=0.0)
    with_fee = futures.liquidation_price("LONG", 100, 1, 10, 100, fee_rate=0.006)
    assert with_fee > no_fee  # 90.909 > 90.3614


def test_liquidation_higher_leverage_is_closer():
    far = futures.liquidation_price("LONG", 100, 1, 2, 100)   # lev 2
    near = futures.liquidation_price("LONG", 100, 1, 20, 100)  # lev 20
    assert near > far  # higher leverage -> liquidation closer to entry


def test_unrealized_and_roe():
    # 10x, 1% favourable move -> 10% ROE
    assert futures.unrealized_pnl("LONG", 100, 1, 101) == pytest.approx(1.0)
    assert futures.roe("LONG", 100, 1, 101, im=10) == pytest.approx(0.10)
    # short profits when price falls
    assert futures.unrealized_pnl("SHORT", 100, 1, 99) == pytest.approx(1.0)
    assert futures.roe("SHORT", 100, 1, 99, im=10) == pytest.approx(0.10)


def test_funding_per_bar():
    assert futures.funding_per_bar(1000, 0.0001, 8) == pytest.approx(0.1)
    assert futures.funding_per_bar(1000, 0.0001, 24) == pytest.approx(0.3)


def test_margin_ratio_grows_toward_liquidation():
    # as price moves against a long, margin ratio rises toward 1
    mr_ok = futures.margin_ratio("LONG", 100, 1, 100, im=10, notional=100)
    mr_bad = futures.margin_ratio("LONG", 100, 1, 92, im=10, notional=100)
    assert mr_bad > mr_ok
