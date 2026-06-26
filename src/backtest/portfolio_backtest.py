"""Futures/regime backtest by replaying the LIVE PortfolioEngine over history.

Uses the exact engine the dashboard/live runner uses (long/short, leverage, funding,
liquidation, trailing, regime switch), so backtest == live. Produces the full metric
report and a rolling per-period breakdown so you can see whether the edge is consistent
across time (the validation the research says matters most before risking money).
"""
from __future__ import annotations

import pandas as pd

from ..risk.manager import RiskManager
from ..strategy.registry import get_strategy
from .backtester import BTConfig, compute_metrics


def _align(dfs: dict) -> dict:
    """Trim all symbols to a common start AND common length so positional index == the same
    date across coins, and every consumer (strategy + benchmarks) measures the same window."""
    common_start = max(d["timestamp"].iloc[0] for d in dfs.values())
    out = {s: d[d["timestamp"] >= common_start].reset_index(drop=True) for s, d in dfs.items()}
    n = min((len(d) for d in out.values()), default=0)
    return {s: d.iloc[:n].reset_index(drop=True) for s, d in out.items()}


def run_portfolio_backtest(symbols, strat_name, settings, cfg: BTConfig, leverage, dfs: dict):
    from ..runner.engine import PortfolioEngine

    dfs = _align(dfs)
    strategies = {s: get_strategy(strat_name, settings) for s in symbols}
    eng = PortfolioEngine(strategies, RiskManager(settings), cfg, leverage=leverage, funding_rate=settings_funding(settings))
    ref = dfs[symbols[0]]
    n = min(len(d) for d in dfs.values())
    warmup = max(st.warmup_bars for st in strategies.values())

    eq_ts, eq_val, bim = [], [], 0
    for i in range(n):
        for s in symbols:
            eng.step(s, dfs[s].iloc[: i + 1])
        eq_ts.append(ref.iloc[i]["timestamp"])
        eq_val.append(eng.equity())
        if i >= warmup and len(eng.positions) > 0:
            bim += 1

    equity = pd.Series(eq_val, index=pd.DatetimeIndex(eq_ts), name="equity")
    eq_m = equity.iloc[warmup:] if len(equity) > warmup else equity
    total = max(len(equity) - warmup, 1)
    metrics = compute_metrics(eq_m, eng.trades, bim, total)
    metrics["liquidations"] = eng.liquidations
    metrics["funding_total"] = round(eng.funding_total, 4)
    return equity, eng.trades, metrics, warmup


def settings_funding(settings) -> float:
    return getattr(settings, "funding_rate_estimate", 0.0001)


def rolling_report(equity: pd.Series, trades: list, warmup: int, n_windows: int = 4) -> list[dict]:
    """Split the post-warmup equity into n_windows consecutive periods; metrics per period.
    Shows whether returns are consistent or driven by one lucky stretch."""
    eq = equity.iloc[warmup:]
    if len(eq) < n_windows * 2:
        return []
    bounds = [int(len(eq) * k / n_windows) for k in range(n_windows + 1)]
    rows = []
    for k in range(n_windows):
        seg = eq.iloc[bounds[k]: bounds[k + 1] + 1]
        if len(seg) < 2:
            continue
        lo, hi = seg.index[0], seg.index[-1]
        seg_trades = [t for t in trades if lo <= pd.Timestamp(t.exit_time) <= hi]
        m = compute_metrics(seg, seg_trades, 0, len(seg))
        rows.append({
            "from": str(lo.date()), "to": str(hi.date()),
            "return_pct": round(m["total_return"] * 100, 2),
            "max_dd_pct": round(m["max_drawdown"] * 100, 2),
            "trades": m["num_trades"],
            "sharpe": round(m.get("sharpe", float("nan")), 2),
        })
    return rows
