"""Pre-real-money validation suite.

Answers the only questions that matter before risking capital:
  1. Does the strategy beat the honest alternatives (buy & hold, DCA)?
  2. Does the edge survive higher (2x) costs?
  3. What is the RANGE of outcomes (Monte Carlo) — not one lucky path?
If it fails these, the honest move is DCA, not the bot (per the research).
"""
from __future__ import annotations

import numpy as np

from .portfolio_backtest import _align


def buy_hold_return(dfs: dict, initial: float) -> float:
    dfs = _align(dfs)
    n = len(dfs)
    total = sum((initial / n) * (d["close"].iloc[-1] / d["close"].iloc[0]) for d in dfs.values())
    return total / initial - 1.0


def dca_return(dfs: dict, initial: float, every_bars: int = 7) -> float:
    dfs = _align(dfs)
    syms = list(dfs)
    L = min(len(d) for d in dfs.values())
    buys = list(range(0, L, every_bars))
    if not buys:
        return 0.0
    spend_each = initial / (len(buys) * len(syms))
    units = {s: 0.0 for s in syms}
    invested = 0.0
    for i in buys:
        for s in syms:
            units[s] += spend_each / dfs[s]["close"].iloc[i]
            invested += spend_each
    value = sum(units[s] * dfs[s]["close"].iloc[L - 1] for s in syms)
    return value / invested - 1.0 if invested else 0.0


def monte_carlo(trades: list, initial: float, n: int = 3000) -> dict:
    """Bootstrap-resample the per-trade PnLs to get a distribution of outcomes (additive)."""
    pnls = np.array([t.pnl for t in trades], dtype=float)
    if len(pnls) < 5:
        return {}
    rng = np.random.default_rng(12345)
    finals, maxdds = [], []
    for _ in range(n):
        sample = pnls[rng.integers(0, len(pnls), len(pnls))]
        path = initial + np.cumsum(sample)
        finals.append(path[-1])
        peak = np.maximum.accumulate(np.concatenate([[initial], path]))
        dd = (peak[1:] - path) / peak[1:]
        maxdds.append(float(dd.max()) if len(dd) else 0.0)
    finals = np.array(finals)
    rets = finals / initial - 1.0
    return {
        "median_return": float(np.median(rets)),
        "p05_return": float(np.percentile(rets, 5)),
        "p95_return": float(np.percentile(rets, 95)),
        "prob_profit": float((finals > initial).mean()),
        "median_maxdd": float(np.median(maxdds)),
        "p95_maxdd": float(np.percentile(maxdds, 95)),
    }
