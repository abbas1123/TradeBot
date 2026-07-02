"""Engine-level invariants for the futures PortfolioEngine."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from src.backtest.backtester import BTConfig
from src.risk.manager import RiskManager
from src.runner.engine import LevPosition, PortfolioEngine
from src.strategy.base import Action, Signal


def _engine(leverage=10.0, capital=1000.0):
    settings = SimpleNamespace(
        live_capital_cap=1e12,
        risk_per_trade_pct=1.0,
        max_open_positions=10,
        max_daily_loss_pct=5.0,
        max_consecutive_errors=5,
    )
    cfg = BTConfig(initial_capital=capital, fee_rate=0.001, slippage_bps=0.0, min_notional=5.0)
    return PortfolioEngine({}, RiskManager(settings), cfg, leverage=leverage)


def test_gap_liquidation_loss_capped_at_margin():
    """A gap-through liquidation must never lose more than the posted margin, and must
    never drive the shared cash pool negative (isolated-margin invariant)."""
    eng = _engine(leverage=10.0, capital=1000.0)
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    # open a LONG manually: notional 200, im 20
    eng.cash -= 20.2  # lock margin + entry fee
    eng.positions["BTC/USDT"] = LevPosition(
        "BTC/USDT", "LONG", entry_price=100.0, qty=2.0, im=20.0, leverage=10.0,
        stop_price=95.0, target_price=None, entry_ts=ts, entry_fee=0.2, liq=90.0,
    )
    cash_before = eng.cash  # 979.8

    # bar gaps far below the liquidation price (price 50 << liq 90)
    eng._close("BTC/USDT", 50.0, ts, "liquidation")

    assert eng.cash >= cash_before - 1e-9          # liquidation returns >= 0 to the pool
    assert eng.equity() >= 0.0                       # never negative equity
    assert eng.realized_pnl >= -(20.0 + 0.2) - 1e-9  # loss capped at margin + entry fee
    assert eng.liquidations == 1


def _long(eng, entry=100.0, stop=96.0, atr=2.0):
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    eng.positions["X/USDT"] = LevPosition(
        "X/USDT", "LONG", entry_price=entry, qty=1.0, im=33.3, leverage=3.0,
        stop_price=stop, target_price=None, entry_ts=ts, entry_fee=0.0, liq=70.0,
        peak=entry, atr_entry=atr, init_stop=stop,
    )
    return eng.positions["X/USDT"]


def test_trailing_breakeven_after_1R():
    eng = _engine()
    p = _long(eng)  # entry 100, stop 96 -> risk 4 (1R)
    eng._update_trailing(p, 104.5, 104.5)  # up 4.5 = 1.1R -> break-even floor
    assert p.stop_price == pytest.approx(100.0)  # stop pulled to entry; can't lose now


def test_trailing_chandelier_locks_profit_and_ratchets_only():
    eng = _engine()
    p = _long(eng)
    eng._update_trailing(p, 110.0, 110.0)  # peak 110 - 2.5*2 = 105 (> break-even 100)
    assert p.stop_price == pytest.approx(105.0)
    assert p.stop_price > p.entry_price  # profit locked
    eng._update_trailing(p, 105.5, 105.5)  # pulled back; stop must NOT loosen
    assert p.stop_price == pytest.approx(105.0)


def test_entry_gate_cooldown_and_same_side():
    eng = _engine()
    eng.cfg.min_edge_mult = 0.0  # isolate the cooldown/same-side checks
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    sig = Signal(Action.BUY, "x", ts, 100.0, stop_price=95.0, atr=5.0)
    assert eng._entry_allowed("BTC/USDT", sig, "LONG")
    eng._cooldown["BTC/USDT"] = 2  # recent exit -> blocked
    assert not eng._entry_allowed("BTC/USDT", sig, "LONG")
    # same-direction cap
    eng._cooldown.clear()
    eng.cfg.max_same_side = 1
    eng.positions["ETH/USDT"] = LevPosition("ETH/USDT", "LONG", 1.0, 1.0, 1.0, 1.0, 0.5, None, ts, 0.0, 0.0)
    assert not eng._entry_allowed("BTC/USDT", sig, "LONG")  # already 1 long
    assert eng._entry_allowed("BTC/USDT", sig, "SHORT")  # other side still allowed


def test_entry_gate_min_edge_floor():
    eng = _engine()
    eng.cfg.min_edge_mult = 2.0  # threshold = 2 * (2*fee + 2*slip)
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    thin = Signal(Action.BUY, "x", ts, 100.0, stop_price=99.99, atr=0.01)  # edge ~0.01% << cost
    wide = Signal(Action.BUY, "x", ts, 100.0, stop_price=90.0, atr=5.0)  # edge 5% >> cost
    assert not eng._entry_allowed("X/USDT", thin, "LONG")
    assert eng._entry_allowed("X/USDT", wide, "LONG")


def test_normal_close_pnl_accounting():
    """A profitable long close credits cash by margin + price PnL - exit fee."""
    eng = _engine(leverage=5.0, capital=1000.0)
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    eng.cash -= 20.0  # lock im for a notional-100 position at 5x
    eng.positions["ETH/USDT"] = LevPosition(
        "ETH/USDT", "LONG", entry_price=100.0, qty=1.0, im=20.0, leverage=5.0,
        stop_price=90.0, target_price=None, entry_ts=ts, entry_fee=0.0, liq=80.0,
    )
    eng._close("ETH/USDT", 110.0, ts, "signal")  # +10 price move on qty 1
    exit_fee = 110.0 * 1.0 * 0.001
    # cash = 980 (after lock) + im 20 + pricePnL 10 - exit_fee
    assert eng.cash == 980.0 + 20.0 + 10.0 - exit_fee
    assert eng.realized_pnl == 10.0 - exit_fee


# --- state persistence (survives once-per-run jobs like GitHub Actions) ---------------

def _trade(ts, pnl=9.8):
    from src.backtest.backtester import Trade

    return Trade(entry_time=ts, entry_price=100.0, exit_time=ts, exit_price=110.0,
                 qty=1.0, fees=0.2, pnl=pnl, pnl_pct=0.49, bars_held=0, exit_reason="signal")


def test_save_load_roundtrip_with_trades(tmp_path):
    from pathlib import Path

    ts = pd.Timestamp("2024-01-01", tz="UTC")
    eng = _engine(leverage=2.0)
    eng.state_path = Path(tmp_path / "state.json")
    eng.cash = 900.0
    eng.trades.append(_trade(ts))
    eng.trades_total = 7  # lifetime counter from earlier runs (> bounded window)
    eng.positions["BTC/USDT"] = LevPosition(
        "BTC/USDT", "LONG", 100.0, 1.0, 50.0, 2.0, 95.0, None, ts, 0.1, 80.0)
    eng.save()

    eng2 = _engine(leverage=2.0)
    eng2.state_path = Path(tmp_path / "state.json")
    assert eng2.load()
    assert eng2.trades_total == 7
    assert len(eng2.trades) == 1
    t = eng2.trades[0]
    assert t.pnl == 9.8 and t.exit_reason == "signal" and t.entry_time == ts
    assert eng2.cash == 900.0 and "BTC/USDT" in eng2.positions


def test_load_tolerates_pre_history_state(tmp_path):
    """State files written before trade persistence (no "trades" key) must still load."""
    import json
    from pathlib import Path

    eng = _engine()
    eng.state_path = Path(tmp_path / "state.json")
    eng.trades.append(_trade(pd.Timestamp("2024-01-01", tz="UTC")))
    eng.save()
    data = json.loads(eng.state_path.read_text(encoding="utf-8"))
    data.pop("trades"), data.pop("trades_total")  # simulate the old schema
    eng.state_path.write_text(json.dumps(data), encoding="utf-8")

    eng2 = _engine()
    eng2.state_path = Path(tmp_path / "state.json")
    assert eng2.load()
    assert eng2.trades == []
    assert eng2.trades_total == data["num_trades"]  # falls back to the legacy counter


def test_load_corrupt_state_backs_up_and_raises(tmp_path):
    """Corrupt state must NOT silently reset the account to initial capital."""
    from pathlib import Path

    p = tmp_path / "state.json"
    p.write_text("{this is not json", encoding="utf-8")
    eng = _engine()
    eng.state_path = Path(p)
    with pytest.raises(RuntimeError):
        eng.load()
    assert list(tmp_path.glob("state.json.corrupt-*"))  # forensic backup left behind


def test_update_mark_triggers_stop_between_bars():
    """Real-time mark update must enforce the stop without waiting for the next bar close
    (this is what lets an hourly cron protect leveraged positions on 1d signals)."""
    eng = _engine()
    _long(eng, entry=100.0, stop=96.0)
    eng.cash -= 33.3
    eng.update_mark("X/USDT", 95.0)  # live tick through the stop
    assert "X/USDT" not in eng.positions
    assert eng.trades[-1].exit_reason == "stop"
