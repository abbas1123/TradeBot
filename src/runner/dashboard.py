"""Live console dashboard + replay / forward-simulation loops (rich-based).

run_replay   — feed historical bars through the SimEngine fast, so you watch trades and
               the balance move in real time (no waiting, no keys).
run_simulate — poll real public prices on a schedule, step the engine when a new bar
               closes, and refresh a live dashboard (fake balance, no keys).
Both fall back to plain periodic text when --no-watch is used (good for headless runs).
"""
from __future__ import annotations

import time

import pandas as pd


def _money(x: float) -> str:
    c = "green" if x >= 0 else "red"
    return f"[{c}]{x:+,.2f}[/]"


def render(engine, info: dict):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    eq = engine.equity()
    ret = engine.total_return() * 100
    pos = engine.position

    head = Text(
        f" {engine.symbol}   strategy={info.get('strategy')}   tf={info.get('timeframe')}   mode={info.get('mode')} ",
        style="bold white on blue",
    )

    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="cyan", no_wrap=True)
    t.add_column()
    t.add_row("Equity", f"[bold]{eq:,.2f}[/] USDT   ({ret:+.2f}%)")
    t.add_row("Cash", f"{engine.cash:,.2f} USDT")
    t.add_row("Realized PnL", _money(engine.realized_pnl) + " USDT")
    t.add_row("Mark price", f"{engine.mark_price:,.2f}")
    if pos.is_long:
        t.add_row("Position", f"[bold green]LONG[/] {pos.quantity:.6f}")
        t.add_row("Entry / Stop", f"{pos.entry_price:,.2f} / {pos.stop_price:,.2f}")
        t.add_row("Unrealized", _money(engine.unrealized()) + " USDT")
    else:
        t.add_row("Position", "[yellow]FLAT[/] (holding cash)")
    if engine.pending:
        t.add_row("Pending", f"[magenta]{engine.pending[0].upper()} fills next bar[/]")
    if engine.last_signal:
        s = engine.last_signal
        t.add_row("Last signal", f"{s.action.value} - {s.reason}")
    t.add_row("Trades", str(len(engine.trades)))
    t.add_row("Status", info.get("status", ""))

    events = Text("\n".join(list(engine.events)[-12:]) or "(no activity yet)")
    return Group(
        head,
        Panel(t, title="Portfolio", border_style="blue"),
        Panel(events, title="Recent activity", border_style="grey50"),
    )


def _print_summary(engine):
    print(f"\nEquity: {engine.equity():,.2f} USDT  ({engine.total_return()*100:+.2f}%)")
    print(f"Cash: {engine.cash:,.2f}  Realized PnL: {engine.realized_pnl:+.2f}  Trades: {len(engine.trades)}")
    if engine.position.is_long:
        p = engine.position
        print(f"Position: LONG {p.quantity:.6f} @ {p.entry_price:.2f}  stop {p.stop_price:.2f}  unreal {engine.unrealized():+.2f}")
    else:
        print("Position: FLAT (holding cash)")
    print("Recent activity:")
    for line in list(engine.events)[-15:]:
        print("  " + line)


def run_replay(engine, df: pd.DataFrame, info: dict, watch: bool = True, speed: float = 30.0):
    warmup = max(getattr(engine.strategy, "warmup_bars", 200), 2)
    total = len(df) - warmup
    if total <= 0:
        print("Not enough data to replay.")
        return

    if watch:
        from rich.console import Console
        from rich.live import Live

        with Live(render(engine, info), refresh_per_second=12, console=Console(), screen=False) as live:
            for i in range(warmup, len(df)):
                engine.step(df.iloc[: i + 1])
                info["status"] = f"replay {i - warmup + 1}/{total}   bar {pd.Timestamp(df.iloc[i]['timestamp']).date()}"
                live.update(render(engine, info))
                if speed > 0:
                    time.sleep(1.0 / speed)
            info["status"] = "replay complete"
            live.update(render(engine, info))
    else:
        for i in range(warmup, len(df)):
            engine.step(df.iloc[: i + 1])
        _print_summary(engine)


def run_simulate(engine, fetcher, symbol: str, timeframe: str, info: dict, watch: bool = True, poll_seconds: float = 5.0, max_ticks: int = 0):
    warmup = max(getattr(engine.strategy, "warmup_bars", 200), 2)

    def tick():
        df = fetcher.fetch_latest(symbol, timeframe, warmup + 5)
        engine.step(df)
        try:
            tk = fetcher.exchange.fetch_ticker(symbol)
            if tk and tk.get("last"):
                engine.mark_price = float(tk["last"])
        except Exception:
            pass
        last = engine.last_processed_ts
        info["status"] = f"polling every {poll_seconds:.0f}s   last closed bar: {last}"

    n = 0
    if watch:
        from rich.live import Live

        with Live(render(engine, info), refresh_per_second=4, screen=False) as live:
            while True:
                tick()
                live.update(render(engine, info))
                n += 1
                if max_ticks and n >= max_ticks:
                    break
                time.sleep(poll_seconds)
    else:
        while True:
            tick()
            print(f"[{engine.last_processed_ts}] equity={engine.equity():,.2f} ({engine.total_return()*100:+.2f}%) pos={engine.position.state} price={engine.mark_price:,.2f}")
            n += 1
            if max_ticks and n >= max_ticks:
                break
            time.sleep(poll_seconds)
