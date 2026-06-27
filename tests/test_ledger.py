"""Balance ledger: the JSON-line writer + the engine open/close hooks that snapshot equity."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from src.backtest.backtester import BTConfig
from src.risk.manager import RiskManager
from src.runner.engine import LevPosition, PortfolioEngine
from src.strategy.base import Action, Signal
from src.utils.ledger import append_ledger


def _engine(capital=1000.0, ledger_path=None):
    settings = SimpleNamespace(
        live_capital_cap=1e12, risk_per_trade_pct=1.0, max_open_positions=10,
        max_daily_loss_pct=5.0, max_consecutive_errors=5,
    )
    cfg = BTConfig(initial_capital=capital, fee_rate=0.001, slippage_bps=0.0, min_notional=5.0)
    return PortfolioEngine({}, RiskManager(settings), cfg, leverage=5.0, ledger_path=ledger_path)


def test_append_ledger_writes_one_json_line_each(tmp_path):
    p = tmp_path / "sub" / "ledger.jsonl"  # parent dir is created
    append_ledger(str(p), {"event": "OPEN", "equity": 1000.0})
    append_ledger(str(p), {"event": "CLOSE", "equity": 1010.0})
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "OPEN"
    assert json.loads(lines[1])["equity"] == 1010.0


def test_append_ledger_noop_on_empty_path():
    append_ledger(None, {"event": "OPEN"})  # must not raise


def test_engine_records_balance_on_close(tmp_path):
    path = tmp_path / "led.jsonl"
    eng = _engine(ledger_path=str(path))
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    eng.cash -= 20.0  # lock margin for a notional-100 position at 5x
    eng.positions["ETH/USDT"] = LevPosition(
        "ETH/USDT", "LONG", entry_price=100.0, qty=1.0, im=20.0, leverage=5.0,
        stop_price=90.0, target_price=None, entry_ts=ts, entry_fee=0.0, liq=80.0,
    )
    eng._close("ETH/USDT", 110.0, ts, "signal")
    assert len(eng.ledger) == 1
    rec = eng.ledger[-1]
    assert rec["event"] == "CLOSE" and rec["symbol"] == "ETH/USDT"
    assert rec["pnl"] > 0 and rec["reason"] == "signal"
    assert rec["equity"] == round(eng.equity(), 2)  # snapshot is the post-close balance
    # and it was persisted to the journal file
    assert json.loads(path.read_text(encoding="utf-8").strip())["event"] == "CLOSE"


def test_engine_records_balance_on_open():
    """The full open path (pending staged, then filled on the next bar's open) logs a record."""
    eng = _engine()
    eng.cfg.min_edge_mult = 0.0
    ts0 = pd.Timestamp("2024-01-01", tz="UTC")
    sig = Signal(Action.BUY, "breakout", ts0, 100.0, stop_price=95.0, atr=5.0)
    eng.pending["BTC/USDT"] = ("open", sig, "LONG")
    eng.strategies["BTC/USDT"] = SimpleNamespace(
        generate_signal=lambda d, p: Signal(Action.HOLD, "", d.iloc[-1]["timestamp"], 100.5),
        warmup_bars=2,
    )
    df = pd.DataFrame({
        "timestamp": [pd.Timestamp("2024-01-02", tz="UTC")],
        "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5],
    })
    eng._step("BTC/USDT", df)
    assert "BTC/USDT" in eng.positions
    opens = [r for r in eng.ledger if r["event"] == "OPEN"]
    assert opens and opens[-1]["symbol"] == "BTC/USDT" and opens[-1]["side"] == "LONG"
    # snapshot is taken at the instant of open (before this bar's tiny funding accrual)
    assert opens[-1]["equity"] == pytest.approx(eng.equity(), abs=1.0)
