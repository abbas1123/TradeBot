"""Event-driven (bar-by-bar) backtester.

It reuses the SAME Strategy.generate_signal() that the live runner uses, so backtest and
live cannot diverge. Decisions are made on a closed bar i and executed on bar i+1's open
(no lookahead); the hard ATR stop is a resting order checked intrabar against the bar's
low via the shared exits.check_level_exit(). Fees apply on every side; slippage is added
to every fill.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..strategy.base import Action, PositionState
from ..strategy.exits import check_level_exit


@dataclass
class BTConfig:
    initial_capital: float = 400.0
    fee_rate: float = 0.001  # per side, as a fraction (0.1% = 0.001)
    slippage_bps: float = 5.0
    min_notional: float = 5.0
    lot_step: float | None = None
    # profit-protection (used by the live PortfolioEngine, not the vectorized backtester)
    use_trailing: bool = True
    trail_atr_mult: float = 2.5  # chandelier: trail stop this many ATRs below the peak
    breakeven_r: float = 1.0  # move stop to break-even once up this many R (initial risks)
    max_position_pct: float = 0.2  # cap each position's margin at this fraction of equity (diversification)


@dataclass
class Trade:
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    qty: float
    fees: float
    pnl: float
    pnl_pct: float
    bars_held: int
    exit_reason: str


@dataclass
class BacktestResult:
    equity: pd.Series  # mark-to-market equity indexed by timestamp
    trades: list[Trade]
    metrics: dict
    df: pd.DataFrame = field(repr=False, default=None)


class Backtester:
    def __init__(self, strategy, risk, cfg: BTConfig):
        self.strategy = strategy
        self.risk = risk
        self.cfg = cfg

    def run(self, df: pd.DataFrame, symbol: str = "?") -> BacktestResult:
        df = df.reset_index(drop=True)
        slip = self.cfg.slippage_bps / 1e4
        fee_rate = self.cfg.fee_rate
        cap = self.cfg.initial_capital

        position = PositionState()
        entry_meta = {}  # entry_i, entry_fee
        pending = None  # ("buy", signal) | ("sell", reason)
        trades: list[Trade] = []
        eq_ts: list[pd.Timestamp] = []
        eq_val: list[float] = []
        bars_in_market = 0

        warmup = max(getattr(self.strategy, "warmup_bars", 200), 2)

        def close_position(fill_px, ts, i, reason):
            nonlocal cap, position, entry_meta
            qty = position.quantity
            proceeds = fill_px * qty
            fee = proceeds * fee_rate
            cap += proceeds - fee
            entry_fee = entry_meta.get("entry_fee", 0.0)
            gross = (fill_px - position.entry_price) * qty
            total_fees = entry_fee + fee
            pnl = gross - total_fees
            pnl_pct = pnl / (position.entry_price * qty) if position.entry_price and qty else 0.0
            trades.append(
                Trade(
                    entry_time=position.entry_ts,
                    entry_price=position.entry_price,
                    exit_time=ts,
                    exit_price=fill_px,
                    qty=qty,
                    fees=total_fees,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    bars_held=i - entry_meta.get("entry_i", i),
                    exit_reason=reason,
                )
            )
            position = PositionState()
            entry_meta = {}

        for i in range(len(df)):
            bar = df.iloc[i]
            o, h, l, c, ts = (
                float(bar["open"]),
                float(bar["high"]),
                float(bar["low"]),
                float(bar["close"]),
                bar["timestamp"],
            )

            # (1) fill any pending action from the previous bar, at THIS bar's open
            if pending is not None:
                kind = pending[0]
                if kind == "buy" and position.is_flat:
                    sig = pending[1]
                    fill_px = o * (1 + slip)
                    stop = sig.stop_price
                    target = sig.target_price
                    sized = self.risk.size_position(
                        capital_available=cap,
                        entry_price=sig.bar_close,
                        stop_price=stop,
                        min_notional=self.cfg.min_notional,
                        lot_step=self.cfg.lot_step,
                    )
                    if sized.approved:
                        cost = fill_px * sized.quantity
                        fee = cost * fee_rate
                        if cost + fee <= cap:
                            cap -= cost + fee
                            position = PositionState(
                                state="LONG",
                                symbol=symbol,
                                entry_price=fill_px,
                                quantity=sized.quantity,
                                stop_price=stop,
                                target_price=target,
                                entry_ts=ts,
                            )
                            entry_meta = {"entry_i": i, "entry_fee": fee}
                elif kind == "sell" and position.is_long:
                    fill_px = o * (1 - slip)
                    close_position(fill_px, ts, i, pending[1])
                pending = None

            # (2) manage open position: intrabar stop/target (resting order)
            if position.is_long:
                hit = check_level_exit(position, h, l)
                if hit is not None:
                    reason, level = hit
                    # gap-through: cannot fill better than the open
                    fill_px = (min(level, o) if reason == "stop" else max(level, o)) * (
                        1 - slip if reason == "stop" else 1 + slip
                    )
                    close_position(fill_px, ts, i, reason)

            # (3) decision on this CLOSED bar -> stage for next bar's open
            if i >= warmup - 1:
                signal = self.strategy.generate_signal(df.iloc[: i + 1], position)
                if signal.action == Action.BUY and position.is_flat and pending is None:
                    pending = ("buy", signal)
                elif signal.action == Action.SELL and position.is_long:
                    pending = ("sell", f"signal:{signal.reason}")

            # (4) mark-to-market equity
            if position.is_long:
                bars_in_market += 1
            mtm = cap + (position.quantity * c if position.is_long else 0.0)
            eq_ts.append(ts)
            eq_val.append(mtm)

        equity = pd.Series(eq_val, index=pd.DatetimeIndex(eq_ts), name="equity")
        metrics = compute_metrics(equity, trades, bars_in_market, len(df))
        return BacktestResult(equity=equity, trades=trades, metrics=metrics, df=df)

    def plot_equity(self, result: BacktestResult, path: str) -> None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        eq = result.equity
        df = result.df
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), height_ratios=[3, 1], sharex=True)

        ax1.plot(eq.index, eq.values, label="Strategy equity", color="tab:blue")
        if df is not None and not df.empty:
            # buy-and-hold benchmark scaled to the same starting capital
            bh = df.set_index("timestamp")["close"]
            bh = bh / bh.iloc[0] * eq.iloc[0]
            ax1.plot(bh.index, bh.values, label="Buy & hold", color="tab:gray", alpha=0.7)
        ax1.set_ylabel("Equity")
        ax1.legend()
        ax1.set_title("Backtest equity vs buy & hold")

        peak = eq.cummax()
        dd = (eq / peak - 1.0) * 100.0
        ax2.fill_between(dd.index, dd.values, 0, color="tab:red", alpha=0.4)
        ax2.set_ylabel("Drawdown %")
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)


def compute_metrics(equity: pd.Series, trades: list[Trade], bars_in_market: int, total_bars: int) -> dict:
    out: dict = {
        "num_trades": len(trades),
        "total_bars": total_bars,
        "exposure": (bars_in_market / total_bars) if total_bars else 0.0,
    }
    if len(equity) < 2 or equity.iloc[0] <= 0:
        out.update({"total_return": 0.0, "cagr": float("nan"), "max_drawdown": 0.0})
        return out

    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    seconds = (equity.index[-1] - equity.index[0]).total_seconds()
    years = seconds / (365.25 * 24 * 3600) if seconds > 0 else 0.0
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if years > 0 else float("nan")

    peak = equity.cummax()
    dd = equity / peak - 1.0
    max_dd = float(dd.min())

    rets = equity.pct_change().dropna()
    # annualization factor from median bar spacing (version-robust delta in seconds)
    if len(equity) >= 3:
        sec = equity.index.to_series().diff().dropna().dt.total_seconds()
        med = float(np.median(sec.to_numpy())) if len(sec) else 0.0
        ppy = (365.25 * 24 * 3600) / med if med > 0 else 365.0
    else:
        ppy = 365.0
    ann = math.sqrt(ppy)

    if len(rets) >= 2 and rets.std(ddof=1) > 0:
        sharpe = rets.mean() / rets.std(ddof=1) * ann
    else:
        sharpe = float("nan")
    downside = rets[rets < 0]
    if len(downside) >= 2 and downside.std(ddof=1) > 0:
        sortino = rets.mean() / downside.std(ddof=1) * ann
    else:
        sortino = float("nan")

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    out.update(
        {
            "total_return": float(total_return),
            "cagr": float(cagr),
            "max_drawdown": max_dd,
            "sharpe": float(sharpe),
            "sortino": float(sortino),
            "win_rate": (len(wins) / len(pnls)) if pnls else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
            "avg_trade": (sum(pnls) / len(pnls)) if pnls else 0.0,
            "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
            "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        }
    )
    return out


def format_metrics(metrics: dict, label: str = "") -> str:
    def pct(x):
        return f"{x*100:.2f}%" if isinstance(x, (int, float)) and not math.isnan(x) else "n/a"

    def num(x, d=2):
        return f"{x:.{d}f}" if isinstance(x, (int, float)) and not math.isnan(x) else "n/a"

    lines = [f"--- Backtest metrics {label} ---".strip()]
    lines.append(f"trades={metrics.get('num_trades')}  exposure={pct(metrics.get('exposure', float('nan')))}")
    lines.append(f"total_return={pct(metrics.get('total_return', float('nan')))}  CAGR={pct(metrics.get('cagr', float('nan')))}")
    lines.append(f"max_drawdown={pct(metrics.get('max_drawdown', float('nan')))}")
    lines.append(f"sharpe={num(metrics.get('sharpe', float('nan')))}  sortino={num(metrics.get('sortino', float('nan')))}")
    lines.append(f"win_rate={pct(metrics.get('win_rate', float('nan')))}  profit_factor={num(metrics.get('profit_factor', float('nan')))}")
    lines.append(f"avg_trade={num(metrics.get('avg_trade', float('nan')))}  avg_win={num(metrics.get('avg_win', float('nan')))}  avg_loss={num(metrics.get('avg_loss', float('nan')))}")
    return "\n".join(lines)
