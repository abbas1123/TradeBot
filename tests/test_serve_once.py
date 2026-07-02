"""_serve_once (the hourly keyless runner): auto-universe must never orphan a position."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from config.settings import Settings
from main import _serve_once
from src.backtest.backtester import BTConfig
from src.risk.manager import RiskManager
from src.runner.engine import LevPosition, PortfolioEngine
from src.strategy.registry import get_strategy


class _FakeFetcher:
    """Serves the same canned CLOSED-bar history for every symbol (no network)."""

    def __init__(self, df):
        self._df = df
        self.exchange = SimpleNamespace(parse_timeframe=lambda tf: 86400)

    def fetch_latest(self, symbol, timeframe, lookback, drop_unclosed=True):
        return self._df.copy()


def test_serve_once_manages_position_outside_universe(tmp_path, make_ohlcv, monkeypatch):
    """A coin that fell out of today's top-N but has an open position must still be
    stepped (strategy auto-created, stop enforced) — not silently frozen forever."""
    import main as main_mod

    monkeypatch.setattr(main_mod, "_write_status_page", lambda *a, **k: None)  # don't touch docs/
    settings = Settings(_env_file=None)
    cfg = BTConfig(initial_capital=1000.0, fee_rate=0.001, slippage_bps=0.0)
    state = Path(tmp_path / "state.json")

    # yesterday's run held BBB/USDT; today's universe only contains AAA/USDT
    seed = PortfolioEngine({"BBB/USDT": get_strategy("donchian_futures", settings)},
                           RiskManager(settings), cfg, leverage=3.0, state_path=state)
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    seed.cash -= 33.4
    seed.positions["BBB/USDT"] = LevPosition(
        "BBB/USDT", "LONG", 100.0, 1.0, 33.3, 3.0, 96.0, None, ts, 0.1, 70.0,
        peak=100.0, atr_entry=2.0, init_stop=96.0)
    seed.save()

    engine = PortfolioEngine({"AAA/USDT": get_strategy("donchian_futures", settings)},
                             RiskManager(settings), cfg, leverage=3.0, state_path=state)
    df = make_ohlcv(list(range(100, 140)))  # 40 closed 1d bars, enough for warmup
    _serve_once(
        engine, ["AAA/USDT"], _FakeFetcher(df), "1d", warmup=35,
        notifier=SimpleNamespace(enabled=False), args=SimpleNamespace(reset=False),
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                               error=lambda *a, **k: None),
        settings=settings, strat_name="donchian_futures",
    )

    assert "BBB/USDT" in engine.strategies          # strategy auto-created for the orphan
    assert engine.last_processed_ts.get("BBB/USDT") is not None  # it was stepped
    # the canned bars (rising to 139) sit above the 96 stop, so the position survives
    assert "BBB/USDT" in engine.positions
