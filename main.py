"""TradeBot CLI entrypoint.

Modes:
    backtest  — historical metrics + equity curve (no keys)
    replay    — fast-forward historical bars through a fake-balance engine with a live
                dashboard, so you watch it trade instantly (no keys)
    simulate  — forward paper trading on REAL public prices with a fake balance + live
                dashboard, on a schedule (no keys)
    status    — print the saved simulation state once
    paper/live — Phase 3+, require your Binance API keys in .env

Examples:
    python main.py --mode backtest --symbol BTC/USDT --regime all
    python main.py --mode replay --symbol BTC/USDT --speed 40
    python main.py --mode replay --timeframe 1h --start 2024-01-01 --speed 60
    python main.py --mode simulate --symbol BTC/USDT --timeframe 1h --poll 5
    python main.py --mode status
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

from config.settings import load_settings
from src.backtest.backtester import Backtester, BTConfig, format_metrics
from src.data.fetcher import Fetcher, build_public_exchange
from src.risk.manager import RiskManager
from src.runner.dashboard import run_replay, run_simulate
from src.runner.engine import PortfolioEngine, SimEngine
from src.strategy.registry import available_strategies, get_strategy
from src.utils.logger import setup_logging

REGIMES = {
    "bull": ("2020-10-01", "2021-04-15"),
    "bear": ("2022-01-01", "2022-12-31"),
    "sideways": ("2019-04-01", "2019-12-31"),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradingbot")
    p.add_argument("--mode", choices=["backtest", "replay", "simulate", "serve", "optimize", "validate", "status", "paper", "live"], default="backtest")
    p.add_argument("--once", action="store_true", help="single iteration (paper/live)")
    p.add_argument("--symbol", default=None, help="e.g. BTC/USDT")
    p.add_argument("--symbols", default=None, help="serve: comma list, or 'auto' for top-volume coins")
    p.add_argument("--top", type=int, default=20, help="serve: scan the top-N most liquid USDT coins when --symbols is auto/unset")
    p.add_argument("--strategy", default=None, choices=available_strategies())
    p.add_argument("--timeframe", default=None, help="e.g. 1d, 1h, 15m")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--split", default=None, help="in/out-of-sample split date (backtest)")
    p.add_argument("--regime", choices=["bull", "bear", "sideways", "all"], default=None)
    p.add_argument("--windows", type=int, default=4, help="futures backtest: rolling consistency periods")
    p.add_argument("--refresh-data", action="store_true", help="ignore OHLCV cache")
    # simulation / dashboard
    p.add_argument("--watch", action=argparse.BooleanOptionalAction, default=True, help="live dashboard (use --no-watch for plain text)")
    p.add_argument("--speed", type=float, default=30.0, help="replay bars per second")
    p.add_argument("--poll", type=float, default=5.0, help="simulate poll interval seconds")
    p.add_argument("--max-ticks", type=int, default=0, help="simulate: stop after N polls (0=run forever)")
    p.add_argument("--capital", type=float, default=None, help="override starting fake balance")
    p.add_argument("--state", default="data_store/sim_state.json", help="simulation state file")
    p.add_argument("--reset", action="store_true", help="start simulation fresh (ignore saved state)")
    p.add_argument("--i-understand-live", action="store_true", help="required to place REAL-money orders in --mode live")
    # web dashboard (serve mode)
    p.add_argument("--port", type=int, default=8000, help="serve: web dashboard port")
    p.add_argument("--open", action=argparse.BooleanOptionalAction, default=True, help="serve: auto-open browser")
    p.add_argument("--source", choices=["live", "replay"], default="live", help="serve: drive engine with live prices or accelerated replay")
    p.add_argument("--duration", type=float, default=0, help="serve(live): session length in minutes (0=unlimited)")
    p.add_argument("--leverage", type=float, default=1.0, help="serve: leverage (1=spot; >1 adds liquidation risk)")
    p.add_argument("--funding", type=float, default=0.0001, help="serve: funding rate per 8h (perpetual); 0 disables")
    p.add_argument("--risk", type=float, default=None, help="override RISK_PER_TRADE_PCT (e.g. 2)")
    p.add_argument("--trail", action=argparse.BooleanOptionalAction, default=False, help="serve: ratcheting profit-protection trailing stop (default off — it cuts trend winners early)")
    p.add_argument("--trail-mult", type=float, default=2.5, help="chandelier trail: ATRs below the peak")
    p.add_argument("--breakeven-r", type=float, default=1.0, help="move stop to break-even once up this many R")
    p.add_argument("--max-pos-pct", type=float, default=0.2, help="serve: cap each position's margin at this fraction of equity")
    p.add_argument("--log-level", default="INFO")
    return p


def _slice(df, start, end, warmup, tf_ms):
    out = df
    if start is not None:
        start_ts = pd.Timestamp(start, tz="UTC")
        if warmup and not df.empty:
            start_ts = start_ts - pd.to_timedelta(int(warmup * tf_ms), unit="ms")
        out = out[out["timestamp"] >= start_ts]
    if end is not None:
        out = out[out["timestamp"] <= pd.Timestamp(end, tz="UTC")]
    return out.reset_index(drop=True)


def _common(settings, args):
    symbol = (args.symbol or settings.pairs[0]).upper()
    timeframe = args.timeframe or settings.timeframe
    strat_name = args.strategy or settings.strategy
    exchange = build_public_exchange(settings.exchange, testnet=False)
    fetcher = Fetcher(exchange, cache_dir="data_store/ohlcv")
    cfg = BTConfig(
        initial_capital=args.capital or settings.initial_capital,
        fee_rate=settings.taker_fee_pct / 100.0,
        slippage_bps=settings.slippage_bps,
        min_notional=5.0,
        use_trailing=getattr(args, "trail", False),
        trail_atr_mult=getattr(args, "trail_mult", 2.5),
        breakeven_r=getattr(args, "breakeven_r", 1.0),
        max_position_pct=getattr(args, "max_pos_pct", 0.2),
    )
    return symbol, timeframe, strat_name, exchange, fetcher, cfg


def run_backtest(settings, args, logger):
    symbol, timeframe, strat_name, exchange, fetcher, cfg = _common(settings, args)
    logger.info(f"Fetching OHLCV {symbol} {timeframe} ...")
    df = fetcher.fetch_ohlcv(symbol, timeframe, use_cache=not args.refresh_data)
    if df.empty:
        logger.error("No data fetched; aborting.")
        return
    logger.info(f"Fetched {len(df)} bars: {df['timestamp'].iloc[0].date()} -> {df['timestamp'].iloc[-1].date()}")
    warmup = get_strategy(strat_name, settings).warmup_bars
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    out_dir = Path("data_store/backtests")
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = symbol.replace("/", "-")

    def _one(window_df, label):
        bt = Backtester(get_strategy(strat_name, settings), RiskManager(settings), cfg)
        result = bt.run(window_df, symbol=symbol)
        print(format_metrics(result.metrics, label=label))
        path = out_dir / f"{safe}_{strat_name}_{label or 'full'}.png"
        bt.plot_equity(result, str(path))
        logger.info(f"Saved equity curve -> {path}")

    if args.regime == "all":
        for name, (s, e) in REGIMES.items():
            wdf = _slice(df, s, e, warmup, tf_ms)
            if len(wdf) <= warmup:
                logger.warning(f"regime {name}: not enough data, skipping")
                continue
            _one(wdf, name)
    elif args.regime:
        s, e = REGIMES[args.regime]
        _one(_slice(df, s, e, warmup, tf_ms), args.regime)
    elif args.split:
        _one(_slice(df, args.start, args.split, warmup, tf_ms), "in_sample")
        _one(_slice(df, args.split, args.end, warmup, tf_ms), "out_of_sample")
    else:
        _one(_slice(df, args.start, args.end, warmup, tf_ms), "full")


def run_futures_backtest(settings, args, logger):
    """Backtest the long/short futures + regime strategies via the live PortfolioEngine."""
    from src.backtest.portfolio_backtest import rolling_report, run_portfolio_backtest

    _, timeframe, _, exchange, fetcher, cfg = _common(settings, args)
    strat_name = args.strategy or "regime"
    if args.symbols and args.symbols.lower() != "auto":
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.symbol:
        symbols = [args.symbol.upper()]
    else:
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]  # fast default for backtest
    logger.info(f"Futures backtest: {', '.join(symbols)} {timeframe} · {strat_name} · {args.leverage:g}x · risk {settings.risk_per_trade_pct}%")

    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    dfs = {}
    for s in symbols:
        d = fetcher.fetch_ohlcv(s, timeframe, use_cache=not args.refresh_data)
        d = _slice(d, args.start, args.end, 0, tf_ms)
        if not d.empty:
            dfs[s] = d
    if not dfs:
        logger.error("No data fetched; aborting.")
        return

    equity, trades, metrics, warmup = run_portfolio_backtest(list(dfs.keys()), strat_name, settings, cfg, args.leverage, dfs)
    from src.backtest.backtester import format_metrics

    print(format_metrics(metrics, label=f"{strat_name} x{args.leverage:g}"))
    print(f"  liquidations={metrics.get('liquidations')}  funding_total={metrics.get('funding_total')}")
    rows = rolling_report(equity, trades, warmup, n_windows=args.windows)
    if rows:
        print("--- rolling periods (is the edge consistent across time?) ---")
        for r in rows:
            print(f"  {r['from']}..{r['to']}  return={r['return_pct']:+.2f}%  maxDD={r['max_dd_pct']:.2f}%  trades={r['trades']}  sharpe={r['sharpe']}")
        pos = sum(1 for r in rows if r["return_pct"] > 0)
        print(f"  -> {pos}/{len(rows)} periods positive  (consistency signal)")

    # save equity curve
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from pathlib import Path

        out = Path("data_store/backtests")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"futures_{strat_name}_x{args.leverage:g}.png"
        fig, ax = plt.subplots(figsize=(11, 5))
        ax.plot(equity.index, equity.values, color="tab:blue")
        ax.axhline(cfg.initial_capital, ls="--", color="grey", alpha=0.6)
        ax.set_title(f"{strat_name} x{args.leverage:g} equity")
        fig.tight_layout()
        fig.savefig(str(path), dpi=110)
        plt.close(fig)
        logger.info(f"Saved equity curve -> {path}")
    except Exception as e:
        logger.warning(f"plot skipped: {e}")


def run_replay_mode(settings, args, logger):
    symbol, timeframe, strat_name, exchange, fetcher, cfg = _common(settings, args)
    logger.info(f"Fetching OHLCV {symbol} {timeframe} for replay ...")
    df = fetcher.fetch_ohlcv(symbol, timeframe, use_cache=not args.refresh_data)
    strategy = get_strategy(strat_name, settings)
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    df = _slice(df, args.start, args.end, strategy.warmup_bars, tf_ms)
    if df.empty:
        logger.error("No data for replay.")
        return
    engine = SimEngine(strategy, RiskManager(settings), cfg, symbol, state_path=None)
    info = {"strategy": strat_name, "timeframe": timeframe, "mode": "replay", "status": ""}
    run_replay(engine, df, info, watch=args.watch, speed=args.speed)


def run_simulate_mode(settings, args, logger):
    symbol, timeframe, strat_name, exchange, fetcher, cfg = _common(settings, args)
    strategy = get_strategy(strat_name, settings)
    engine = SimEngine(strategy, RiskManager(settings), cfg, symbol, state_path=args.state)
    if not args.reset and engine.load():
        logger.info(f"Resumed simulation from {args.state}")
    else:
        logger.info(f"Starting fresh simulation with {cfg.initial_capital:.2f} USDT fake balance")
    info = {"strategy": strat_name, "timeframe": timeframe, "mode": "simulate", "status": "starting..."}
    logger.info("Press Ctrl+C to stop (state is saved).")
    run_simulate(engine, fetcher, symbol, timeframe, info, watch=args.watch, poll_seconds=args.poll, max_ticks=args.max_ticks)


def _serve_symbols(settings, args, exchange) -> list[str]:
    if args.symbols and args.symbols.lower() != "auto":
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.symbol:
        return [args.symbol.upper()]
    # no explicit symbols -> scan the top-N most liquid USDT coins
    from src.data.universe import top_symbols
    return top_symbols(exchange, n=args.top, quote="USDT")


def run_serve(settings, args, logger):
    from src.runner.webserver import Monitor, serve

    _, timeframe, _, exchange, fetcher, cfg = _common(settings, args)
    # serve defaults to the regime-switch meta-strategy (trend in trends, mean-reversion
    # in ranges) — the research's "right tool per regime" conclusion
    strat_name = args.strategy or "regime"
    logger.info("Selecting trading universe ...")
    symbols = _serve_symbols(settings, args, exchange)
    strategies = {s: get_strategy(strat_name, settings) for s in symbols}
    state_path = args.state if args.source == "live" else None
    bar_hours = exchange.parse_timeframe(timeframe) / 3600.0
    engine = PortfolioEngine(
        strategies, RiskManager(settings), cfg, leverage=args.leverage,
        funding_rate=args.funding, bar_hours=bar_hours, state_path=state_path,
    )
    warmup = max(st.warmup_bars for st in strategies.values())

    lev_note = f" · leverage {args.leverage:g}x (LIQUIDATION RISK)" if args.leverage > 1 else ""
    logger.info(f"Serve: {', '.join(symbols)} {timeframe} · {strat_name} · ${cfg.initial_capital:,.0f} fake{lev_note}")
    if args.leverage > 1:
        logger.warning("Leverage amplifies BOTH profit and loss; a position is liquidated (margin lost) when its margin ratio hits 100%. Paper money only.")

    info = {"strategy": strat_name, "timeframe": timeframe, "mode": f"serve/{args.source}", "status": "starting..."}
    monitor = Monitor(engine, info)
    from src.utils.notify import Notifier
    notifier = Notifier(settings)
    duration_sec = int(args.duration * 60) if args.duration else 0
    monitor.session_total = duration_sec

    if args.source == "replay":
        logger.info("Fetching history for replay ...")
        tf_ms = exchange.parse_timeframe(timeframe) * 1000
        dfs = {}
        for s in symbols:
            d = fetcher.fetch_ohlcv(s, timeframe, use_cache=not args.refresh_data)
            dfs[s] = _slice(d, args.start, args.end, warmup, tf_ms)
        min_len = min((len(d) for d in dfs.values()), default=0)
        total = max(min_len - warmup, 1)

        def loop():
            for i in range(warmup, min_len):
                for s in symbols:
                    engine.step(s, dfs[s].iloc[: i + 1])
                info["status"] = f"replay {i - warmup + 1}/{total}"
                monitor.record()
                if args.speed > 0:
                    time.sleep(1.0 / args.speed)
            info["status"] = f"replay complete · final equity {engine.equity():,.2f}"
    else:
        tf_seconds = exchange.parse_timeframe(timeframe)

        def loop():
            monitor.start_session()
            ohlcv_due = {s: 0.0 for s in symbols}
            n_ticks = 0
            while True:
                now = time.time()
                # (a) one batched tickers call -> update marks + real-time risk (liq/stop)
                try:
                    tickers = exchange.fetch_tickers(symbols)
                except Exception as e:
                    tickers = {}
                    info["status"] = f"tickers error: {e}"
                for s in symbols:
                    t = tickers.get(s)
                    if t and t.get("last"):
                        engine.update_mark(s, float(t["last"]))
                # (b) decisions on fresh bars, throttled per coin (avoids hammering OHLCV)
                for s in symbols:
                    if now >= ohlcv_due[s]:
                        try:
                            df = fetcher.fetch_latest(s, timeframe, warmup + 5)
                            engine.step(s, df)
                        except Exception:
                            pass
                        ohlcv_due[s] = now + max(tf_seconds * 0.5, args.poll)
                if duration_sec:
                    left = max(0, duration_sec - int(time.time() - monitor.session_start))
                    info["status"] = f"live · {left//60}m{left%60:02d}s left · {len(engine.positions)}/{len(symbols)} positions"
                else:
                    info["status"] = f"live · {len(symbols)} coins · {len(engine.positions)} positions open"
                monitor.record()
                n_ticks += 1
                if notifier.enabled and n_ticks % 60 == 0:  # periodic Telegram position report
                    rows = []
                    for sym in symbols:
                        p = engine.positions.get(sym)
                        if p:
                            mk = engine.marks.get(sym, p.entry_price)
                            rows.append((sym, p.side, p.entry_price, mk, p.unrealized(mk)))
                    notifier.report(engine.equity(), engine.cash, rows, extra=f"{len(engine.positions)} open")
                if duration_sec and (time.time() - monitor.session_start) >= duration_sec:
                    info["status"] = f"session complete ({args.duration:.0f} min) · final equity {engine.equity():,.2f}"
                    engine.save()
                    break
                time.sleep(args.poll)

    serve(monitor, loop, host="127.0.0.1", port=args.port, open_browser=args.open, logger=logger)


def run_validate(settings, args, logger):
    """The decisive pre-real-money test: beat buy&hold/DCA, survive 2x costs, MC range."""
    import dataclasses

    from src.backtest.portfolio_backtest import run_portfolio_backtest
    from src.backtest.validate import buy_hold_return, dca_return, monte_carlo

    _, timeframe, _, exchange, fetcher, cfg = _common(settings, args)
    strat_name = args.strategy or "regime"
    if args.symbols and args.symbols.lower() != "auto":
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.symbol:
        symbols = [args.symbol.upper()]
    else:
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    dfs = {}
    for s in symbols:
        d = _slice(fetcher.fetch_ohlcv(s, timeframe, use_cache=not args.refresh_data), args.start, args.end, 0, tf_ms)
        if not d.empty:
            dfs[s] = d
    if not dfs:
        logger.error("No data; aborting.")
        return
    init = cfg.initial_capital
    logger.info(f"Validating {strat_name} on {', '.join(dfs)} {timeframe} x{args.leverage:g} ...")

    _, trades, m, _ = run_portfolio_backtest(list(dfs), strat_name, settings, cfg, args.leverage, dfs)
    strat_ret = m["total_return"]
    bh = buy_hold_return(dfs, init)
    dca = dca_return(dfs, init)

    cfg2 = dataclasses.replace(cfg, fee_rate=cfg.fee_rate * 2, slippage_bps=cfg.slippage_bps * 2)
    _, _, m2, _ = run_portfolio_backtest(list(dfs), strat_name, settings, cfg2, args.leverage, dfs)
    cost_ret = m2["total_return"]
    mc = monte_carlo(trades, init)

    def pct(x):
        return f"{x*100:+.2f}%"

    print("\n================  PRE-REAL-MONEY VALIDATION  ================")
    print(f"Strategy ({strat_name} x{args.leverage:g}):  return {pct(strat_ret)}  maxDD {pct(m['max_drawdown'])}  Sharpe {m.get('sharpe', float('nan')):.2f}  trades {m['num_trades']}")
    print("\n-- vs honest alternatives (same period) --")
    print(f"  Buy & hold:   {pct(bh)}   -> strategy {'BEATS' if strat_ret > bh else 'LOSES TO'} buy & hold")
    print(f"  DCA (weekly): {pct(dca)}   -> strategy {'BEATS' if strat_ret > dca else 'LOSES TO'} DCA")
    print("\n-- cost stress (2x fees + 2x slippage) --")
    print(f"  return at 2x costs: {pct(cost_ret)}   -> edge {'SURVIVES' if cost_ret > 0 else 'DIES'} higher costs")
    if mc:
        print("\n-- Monte Carlo (bootstrap of trades, range of outcomes) --")
        print(f"  median {pct(mc['median_return'])} | 5th pct {pct(mc['p05_return'])} | 95th pct {pct(mc['p95_return'])}")
        print(f"  probability of profit: {mc['prob_profit']*100:.0f}%   median maxDD {pct(-mc['median_maxdd'])}   bad-case maxDD {pct(-mc['p95_maxdd'])}")
    # verdict
    checks = [strat_ret > bh, strat_ret > dca, cost_ret > 0, (mc.get("prob_profit", 0) > 0.6)]
    passed = sum(checks)
    print("\n-- VERDICT --")
    print(f"  {passed}/4 checks passed (beats B&H, beats DCA, survives 2x costs, MC>60% profit)")
    if passed >= 3:
        print("  -> Reasonable edge. Still paper-trade for weeks before any real money.")
    else:
        print("  -> WEAK/NO edge. The honest move is DCA, not this bot. Do NOT go live.")
    print("============================================================\n")


def run_optimize(settings, args, logger):
    """Walk-forward parameter search: grid-search on a TRAIN split, validate on held-out TEST.
    Picks robust params by return/drawdown — and shows whether they survive out-of-sample."""
    from src.backtest.portfolio_backtest import run_portfolio_backtest

    _, timeframe, _, exchange, fetcher, cfg = _common(settings, args)
    strat_name = args.strategy or "regime"
    symbols = [args.symbol.upper()] if args.symbol else ["BTC/USDT", "ETH/USDT"]
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    full = {}
    for s in symbols:
        d = _slice(fetcher.fetch_ohlcv(s, timeframe, use_cache=not args.refresh_data), args.start, args.end, 0, tf_ms)
        if not d.empty:
            full[s] = d
    if not full:
        logger.error("No data; aborting.")
        return
    cut = min(int(len(d) * 0.7) for d in full.values())
    warm = 220
    train = {s: d.iloc[:cut].reset_index(drop=True) for s, d in full.items()}
    test = {s: d.iloc[max(0, cut - warm):].reset_index(drop=True) for s, d in full.items()}

    grid_entry, grid_stop = [10, 20, 30], [1.5, 2.0, 2.5]
    logger.info(f"Optimizing {strat_name} on {', '.join(full)} — {len(grid_entry)*len(grid_stop)} combos (train), then OOS test ...")
    rows = []
    for de in grid_entry:
        for sm in grid_stop:
            st2 = settings.model_copy(update={"donchian_entry": de, "atr_stop_mult": sm})
            _, _, m, _ = run_portfolio_backtest(list(train), strat_name, st2, cfg, args.leverage, train)
            score = m["total_return"] / (abs(m["max_drawdown"]) + 0.01)
            rows.append((de, sm, m["total_return"] * 100, m["max_drawdown"] * 100, m.get("sharpe", float("nan")), score))
    rows.sort(key=lambda r: r[5], reverse=True)
    print("--- TRAIN grid (sorted by return/drawdown) ---")
    for de, sm, ret, dd, sh, sc in rows:
        print(f"  entry={de:>2} stop={sm:>3}xATR  return={ret:+7.2f}%  maxDD={dd:7.2f}%  sharpe={sh:5.2f}")
    best = rows[0]
    logger.info(f"Best train params: donchian_entry={best[0]}, atr_stop_mult={best[1]}")
    st_best = settings.model_copy(update={"donchian_entry": best[0], "atr_stop_mult": best[1]})
    _, _, mt, _ = run_portfolio_backtest(list(test), strat_name, st_best, cfg, args.leverage, test)
    from src.backtest.backtester import format_metrics

    print(format_metrics(mt, label=f"OUT-OF-SAMPLE (entry={best[0]}, stop={best[1]})"))
    print("  ^ if OOS is much worse than train, the params are overfit — do NOT trust them.")


def run_live(settings, args, logger):
    """Real spot execution on Binance testnet (paper) or live."""
    from src.runner.live import build_context, run_loop, run_once

    if args.mode == "live" and not args.i_understand_live:
        logger.error("Refusing to start live: this trades REAL money. Re-run with --i-understand-live.")
        return
    ctx = build_context(settings)
    mode = settings.mode.value.upper()
    logger.warning(f"{mode} SPOT execution · {', '.join(settings.pairs)} {settings.timeframe} · strategy={settings.strategy} · cap={settings.live_capital_cap}")
    if args.once:
        run_once(ctx)
        logger.info("Single iteration complete.")
    else:
        run_loop(ctx)


def run_status(settings, args, logger):
    from src.runner.dashboard import _print_summary

    symbol, timeframe, strat_name, exchange, fetcher, cfg = _common(settings, args)
    engine = SimEngine(get_strategy(strat_name, settings), RiskManager(settings), cfg, symbol, state_path=args.state)
    if not engine.load():
        logger.error(f"No saved simulation state at {args.state}. Run --mode simulate first.")
        return
    _print_summary(engine)


def main(argv=None):
    args = build_parser().parse_args(argv)
    logger = setup_logging("logs", args.log_level)

    overrides = {"strategy": args.strategy, "timeframe": args.timeframe}
    if args.risk:
        overrides["risk_per_trade_pct"] = args.risk
    if args.mode in ("backtest", "replay", "simulate", "serve", "optimize", "validate", "status"):
        overrides["mode"] = "backtest"  # keyless analysis/sim — dry-run engine
        overrides["live_capital_cap"] = 1e12  # fake balance fully usable in paper modes
        if args.mode == "serve":
            overrides["max_open_positions"] = 1000
    else:  # paper / live -> real execution; Settings enforces keys + mode/testnet interlock
        overrides["mode"] = args.mode

    try:
        settings = load_settings(**overrides)
    except Exception as e:
        logger.error(f"Config error: {e}")
        logger.error("paper needs BINANCE_TESTNET=true + testnet keys; live needs BINANCE_TESTNET=false + live keys, in .env")
        return

    try:
        if args.mode == "backtest":
            futures_strats = {"donchian_futures", "futures", "regime", "mean_reversion"}
            if (args.strategy in futures_strats) or args.leverage > 1:
                run_futures_backtest(settings, args, logger)
            else:
                run_backtest(settings, args, logger)
        elif args.mode == "replay":
            run_replay_mode(settings, args, logger)
        elif args.mode == "simulate":
            run_simulate_mode(settings, args, logger)
        elif args.mode == "serve":
            run_serve(settings, args, logger)
        elif args.mode == "optimize":
            run_optimize(settings, args, logger)
        elif args.mode == "validate":
            run_validate(settings, args, logger)
        elif args.mode == "status":
            run_status(settings, args, logger)
        elif args.mode in ("paper", "live"):
            run_live(settings, args, logger)
    except KeyboardInterrupt:
        logger.info("Stopped by user. State saved.")


if __name__ == "__main__":
    main()
