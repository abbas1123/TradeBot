"""DCA engine: accumulation, average-cost, equity, and the n_buys cap."""
from __future__ import annotations

import pandas as pd

from src.backtest.backtester import BTConfig
from src.runner.dca import DCAEngine


def _eng(n_buys=2, capital=1000.0):
    cfg = BTConfig(initial_capital=capital, fee_rate=0.001, slippage_bps=0.0, min_notional=5.0)
    return DCAEngine(["BTC/USDT", "ETH/USDT"], cfg, n_buys=n_buys)


def test_dca_accumulates_and_holds():
    eng = _eng(n_buys=2)
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    eng.set_mark("BTC/USDT", 100.0)
    eng.set_mark("ETH/USDT", 10.0)
    assert eng.invest_round(ts) is True
    assert eng.cash == 500.0  # half the budget deployed (one of two tranches)
    assert eng.buys_done == 1
    # prices double -> the holdings are worth more, equity rises
    eng.set_mark("BTC/USDT", 200.0)
    eng.set_mark("ETH/USDT", 20.0)
    assert eng.invest_round(ts) is True
    assert eng.cash == 0.0
    assert eng.equity() > 1000.0  # first tranche doubled
    # third call is a no-op (budget fully deployed)
    assert eng.invest_round(ts) is False
    assert eng.buys_done == 2


def test_dca_average_cost():
    eng = _eng(n_buys=2)
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    eng.set_mark("BTC/USDT", 100.0)
    eng.set_mark("ETH/USDT", 100.0)
    eng.invest_round(ts)            # buy BTC & ETH at 100
    eng.set_mark("BTC/USDT", 300.0)  # BTC triples before the next tranche
    eng.invest_round(ts)            # buy more BTC at 300
    p = eng.positions["BTC/USDT"]
    assert 100.0 < p.entry_price < 300.0  # average cost sits between the two buys
    # ledger recorded both buys with an equity snapshot
    assert len(eng.ledger) == 2
    assert all(r["event"] == "BUY" and "equity" in r for r in eng.ledger)


def test_dca_records_ledger_to_file(tmp_path):
    cfg = BTConfig(initial_capital=1000.0, fee_rate=0.001, slippage_bps=0.0, min_notional=5.0)
    path = tmp_path / "dca_ledger.jsonl"
    eng = DCAEngine(["BTC/USDT"], cfg, n_buys=1, ledger_path=str(path))
    eng.set_mark("BTC/USDT", 50.0)
    eng.invest_round(pd.Timestamp("2024-01-01", tz="UTC"))
    assert path.exists() and "BUY" in path.read_text(encoding="utf-8")
