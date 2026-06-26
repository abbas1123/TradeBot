"""Live runner — real spot order execution on Binance (testnet/live).

SPOT, long-or-flat only (you cannot short spot; real leveraged shorting is deliberately
out of scope for real money). One closed-bar decision per pair, routed Strategy ->
RiskManager.approve_* -> Broker.place_order, with idempotency (never act twice on the same
candle), atomic state persistence, and kill-switch checks. `run_once` is a single
iteration (ideal for Windows Task Scheduler); `run_loop` schedules it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from ..data.fetcher import Fetcher
from ..execution.broker import Broker
from ..risk.manager import RiskManager
from ..risk.types import ApprovedOrder
from ..strategy.base import Action, PositionState
from ..strategy.registry import get_strategy
from ..utils.logger import get_logger
from ..utils.notify import Notifier
from ..utils.state import Position, StateStore


def build_context(settings):
    logger = get_logger()
    risk = RiskManager(settings)
    broker = Broker(settings, risk)
    fetcher = Fetcher(broker.exchange, cache_dir=None)
    strategy = get_strategy(settings.strategy or "donchian", settings)
    state = StateStore(settings.state_path)
    notifier = Notifier(settings)
    return SimpleNamespace(
        settings=settings, risk=risk, broker=broker, fetcher=fetcher,
        strategy=strategy, state=state, logger=logger, notifier=notifier,
    )


def _reconcile(ctx) -> None:
    """Clear positions the exchange no longer actually holds (state/exchange desync)."""
    st, ex = ctx.state.state, ctx.broker.exchange
    if ex is None or not st.open_positions:
        return
    try:
        bal = ctx.broker._with_backoff(ex.fetch_balance)
    except Exception as e:
        ctx.logger.warning(f"reconcile skipped: {e}")
        return
    for symbol in list(st.open_positions):
        base = symbol.split("/")[0]
        held = float((bal.get(base, {}) or {}).get("free", 0.0) or 0.0)
        want = st.open_positions[symbol]["quantity"]
        if held < want * 0.5:  # mostly/entirely gone -> stale
            ctx.logger.warning(f"reconcile: clearing stale {symbol} (held {held} < expected {want})")
            del st.open_positions[symbol]
    ctx.state.save()


def _check_drawdown(ctx) -> None:
    """Realized-equity max-drawdown circuit breaker."""
    st, s = ctx.state.state, ctx.settings
    st.peak_pnl = max(st.peak_pnl, st.cumulative_pnl)
    dd = st.peak_pnl - st.cumulative_pnl
    limit = s.live_capital_cap * s.max_drawdown_pct / 100.0
    if dd > limit:
        ctx.risk.trip_kill(f"max_drawdown {dd:.2f} > {limit:.2f}")
        st.kill_switch_active = True
        st.kill_switch_reason = ctx.risk.kill_reason
        ctx.notifier.notify(f"🛑 KILL: max drawdown hit ({dd:.2f} USDT). Bot now manages exits only.")


def run_once(ctx) -> None:
    s, st, log = ctx.settings, ctx.state.state, ctx.logger
    ctx.state.reset_daily_if_needed()
    # restore kill state from disk
    if st.kill_switch_active:
        ctx.risk.manual_kill = True
        ctx.risk.kill_reason = st.kill_switch_reason or "persisted"

    _reconcile(ctx)
    capital = ctx.broker.free_quote()
    warmup = ctx.strategy.warmup_bars
    marks: dict = {}

    for symbol in s.pairs:
        try:
            df = ctx.fetcher.fetch_latest(symbol, s.timeframe, warmup + 5)
            if df is None or df.empty:
                continue
            bar = df.iloc[-1]
            ts, c = bar["timestamp"], float(bar["close"])
            marks[symbol] = c

            last_ts = st.last_processed_ts.get(symbol)
            if last_ts and pd.Timestamp(last_ts) >= ts:
                continue  # idempotent: this candle already handled

            pos = st.open_positions.get(symbol)
            position = (
                PositionState(state="LONG", symbol=symbol, entry_price=pos["entry_price"],
                              quantity=pos["quantity"], stop_price=pos["stop_price"])
                if pos else PositionState(state="FLAT")
            )
            sig = ctx.strategy.generate_signal(df, position)
            log.info(f"{symbol}: {sig.action.value} ({sig.reason})")
            filters = ctx.broker.get_filters(symbol)

            if pos:  # manage / exit the open long
                hit_stop = c <= pos["stop_price"]
                if hit_stop or sig.action == Action.SELL:
                    reason = "stop" if hit_stop else "signal"
                    approved = ctx.risk.approve_exit(symbol, pos["quantity"])
                    fill = ctx.broker.place_order(approved, c)
                    if fill.status == "filled":
                        pnl = (fill.avg_price - pos["entry_price"]) * fill.filled_qty - fill.fee
                        st.realized_daily_pnl += pnl
                        st.cumulative_pnl += pnl
                        del st.open_positions[symbol]
                        ctx.risk.register_pnl(pnl, capital)
                        msg = f"SELL {symbol} {fill.filled_qty} @ {fill.avg_price:.2f} pnl {pnl:+.2f} ({reason})"
                        log.bind(event="trade").info(msg)
                        ctx.notifier.notify(("✅ " if pnl >= 0 else "🔻 ") + msg)
                        _check_drawdown(ctx)
                    else:
                        log.warning(f"{symbol}: exit not filled ({fill.status})")
            elif sig.action == Action.BUY and not ctx.risk.is_killed:
                approved = ctx.risk.approve_entry(
                    symbol, c, sig.stop_price, capital, st.open_positions,
                    min_notional=filters.min_notional, lot_step=filters.step_size,
                )
                if isinstance(approved, ApprovedOrder):
                    fill = ctx.broker.place_order(approved, c)
                    if fill.status == "filled":
                        st.open_positions[symbol] = vars(
                            Position(symbol, fill.filled_qty, fill.avg_price, sig.stop_price, ts.isoformat())
                        )
                        msg = f"BUY {symbol} {fill.filled_qty} @ {fill.avg_price:.2f} stop {sig.stop_price:.2f}"
                        log.bind(event="trade").info(msg)
                        ctx.notifier.notify("🟢 " + msg)
                    else:
                        log.warning(f"{symbol}: entry not filled ({fill.status})")
                else:
                    log.info(f"{symbol}: entry rejected ({approved.reason})")

            st.last_processed_ts[symbol] = ts.isoformat()
            ctx.state.save()
            ctx.risk.record_success()
        except Exception as e:  # one bad pair must not break the others
            ctx.risk.record_error()
            log.error(f"{symbol}: error {e}")
            if ctx.risk.is_killed:
                st.kill_switch_active = True
                st.kill_switch_reason = ctx.risk.kill_reason
                ctx.state.save()

    # Telegram position snapshot at the end of the iteration
    if ctx.notifier.enabled:
        rows = []
        for sym, p in st.open_positions.items():
            mk = marks.get(sym, p["entry_price"])
            rows.append((sym, "LONG", p["entry_price"], mk, (mk - p["entry_price"]) * p["quantity"]))
        eq = capital + sum(r[4] for r in rows)
        ctx.notifier.report(eq, capital, rows, extra=f"dayPnL {st.realized_daily_pnl:+.2f}")


def run_loop(ctx) -> None:
    """Schedule run_once on the bar cadence (Ctrl+C to stop)."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    tf_seconds = ctx.broker.exchange.parse_timeframe(ctx.settings.timeframe) if ctx.broker.exchange else 86400
    ctx.logger.info(f"Live loop every {tf_seconds}s for {', '.join(ctx.settings.pairs)} (Ctrl+C to stop)")
    run_once(ctx)  # act immediately, then on schedule
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(lambda: run_once(ctx), "interval", seconds=max(tf_seconds, 60), misfire_grace_time=120)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        ctx.logger.info("Live loop stopped.")
