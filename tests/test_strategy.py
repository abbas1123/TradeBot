"""Deterministic signal tests for DonchianTrendStrategy (entry + exit transitions)."""
from __future__ import annotations

from types import SimpleNamespace

from src.strategy.base import Action, PositionState
from src.strategy.donchian import DonchianTrendStrategy
from src.strategy.donchian_futures import DonchianFuturesStrategy
from src.strategy.mean_reversion import MeanReversionStrategy
from src.strategy.regime import RegimeSwitchStrategy

FLAT = PositionState(state="FLAT")
LONG = PositionState(state="LONG", entry_price=100.0, quantity=1.0, stop_price=90.0)
SHORT = PositionState(state="SHORT", entry_price=100.0, quantity=1.0, stop_price=110.0)


def test_no_entry_when_regime_off(make_ohlcv, strat_params):
    # descending series -> close below EMA -> no long entry
    closes = list(range(20, 4, -1))
    df = make_ohlcv(closes)
    sig = DonchianTrendStrategy(strat_params).generate_signal(df, FLAT)
    assert sig.action == Action.HOLD


def test_entry_on_breakout_in_uptrend(make_ohlcv, strat_params):
    closes = [10] * 12 + [20]
    df = make_ohlcv(closes)
    sig = DonchianTrendStrategy(strat_params).generate_signal(df, FLAT)
    assert sig.action == Action.BUY
    assert sig.stop_price is not None and sig.stop_price < 20
    assert sig.atr is not None and sig.atr > 0


def test_no_double_entry_when_long(make_ohlcv, strat_params):
    closes = [10] * 12 + [20]
    df = make_ohlcv(closes)
    sig = DonchianTrendStrategy(strat_params).generate_signal(df, LONG)
    assert sig.action != Action.BUY


def test_trailing_exit_on_breakdown(make_ohlcv, strat_params):
    closes = [10, 10, 10, 10, 10, 10, 50, 50, 50, 45]
    df = make_ohlcv(closes)
    sig = DonchianTrendStrategy(strat_params).generate_signal(df, LONG)
    assert sig.action == Action.SELL
    assert "trailing" in sig.reason


def test_regime_hard_exit(make_ohlcv, strat_params):
    closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 5]
    df = make_ohlcv(closes)
    sig = DonchianTrendStrategy(strat_params).generate_signal(df, LONG)
    assert sig.action == Action.SELL
    assert "regime" in sig.reason


def test_hold_mid_trend(make_ohlcv, strat_params):
    closes = list(range(10, 21))  # 10..20 rising
    df = make_ohlcv(closes)
    sig = DonchianTrendStrategy(strat_params).generate_signal(df, LONG)
    assert sig.action == Action.HOLD


def test_determinism(make_ohlcv, strat_params):
    closes = [10] * 12 + [20]
    df = make_ohlcv(closes)
    strat = DonchianTrendStrategy(strat_params)
    a = strat.generate_signal(df, FLAT).to_dict()
    b = strat.generate_signal(df, FLAT).to_dict()
    assert a == b


def test_does_not_mutate_input(make_ohlcv, strat_params):
    closes = [10] * 12 + [20]
    df = make_ohlcv(closes)
    before = df.copy(deep=True)
    DonchianTrendStrategy(strat_params).generate_signal(df, FLAT)
    assert df.equals(before)


# --- DonchianFuturesStrategy (long + short) ---


def test_futures_long_breakout(make_ohlcv, strat_params):
    df = make_ohlcv([10] * 12 + [20])
    sig = DonchianFuturesStrategy(strat_params).generate_signal(df, FLAT)
    assert sig.action == Action.BUY
    assert sig.stop_price < 20


def test_futures_short_breakdown(make_ohlcv, strat_params):
    df = make_ohlcv([20] * 12 + [5])  # downtrend + new low -> short
    sig = DonchianFuturesStrategy(strat_params).generate_signal(df, FLAT)
    assert sig.action == Action.SHORT
    assert sig.stop_price > 5  # stop is ABOVE entry for a short


def test_futures_cover_on_regime_flip(make_ohlcv, strat_params):
    df = make_ohlcv([20] * 9 + [22, 24, 40])  # price jumps back above EMA
    sig = DonchianFuturesStrategy(strat_params).generate_signal(df, SHORT)
    assert sig.action == Action.COVER


def test_futures_no_entry_when_flat_and_quiet(make_ohlcv, strat_params):
    df = make_ohlcv(list(range(10, 21)))  # steady uptrend, last bar not a fresh breakout high
    sig = DonchianFuturesStrategy(strat_params).generate_signal(df, LONG)
    assert sig.action != Action.SHORT  # already long; never opens a short


# --- MeanReversionStrategy (Bollinger) ---


def _mr_params():
    return SimpleNamespace(bb_period=4, bb_std=1.0, atr_period=3, atr_stop_mult=2.0)


def test_mean_reversion_buys_oversold(make_ohlcv):
    df = make_ohlcv([100, 100, 100, 100, 90])  # last close below lower band (1 std)
    sig = MeanReversionStrategy(_mr_params()).generate_signal(df, FLAT)
    assert sig.action == Action.BUY
    assert sig.target_price is not None and sig.target_price > 90  # target = mean


def test_mean_reversion_shorts_overbought(make_ohlcv):
    df = make_ohlcv([100, 100, 100, 100, 110])
    sig = MeanReversionStrategy(_mr_params()).generate_signal(df, FLAT)
    assert sig.action == Action.SHORT


# --- RegimeSwitchStrategy ---


def test_regime_picks_trend_in_trending(make_ohlcv, strat_params):
    df = make_ohlcv([10] * 12 + [20])  # clean breakout -> low choppiness -> trend tool
    sig = RegimeSwitchStrategy(strat_params).generate_signal(df, FLAT)
    assert "trend" in sig.reason
    assert sig.action == Action.BUY


def test_regime_picks_range_in_choppy(make_ohlcv, strat_params):
    df = make_ohlcv([95, 105, 95, 105, 95, 105, 95, 105])  # zig-zag -> high choppiness
    sig = RegimeSwitchStrategy(strat_params).generate_signal(df, FLAT)
    assert "range" in sig.reason
