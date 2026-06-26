# TradeBot — Personal Spot Crypto Trading Bot (Binance)

A safety-first, test-driven spot trading bot for Binance. Three modes: **backtest**,
**paper** (Binance Spot Testnet), and small **live**. Success is defined by *system
correctness*, not profit — see [docs/trading-bot-spec.md](docs/trading-bot-spec.md).

> ⚠️ Trading is high risk. You can lose all allocated capital. This is a personal,
> educational project, not financial advice. Below ~500 USDT, fees and minimum order
> sizes dominate. Validate on backtest + testnet before risking any real money.

## Strategies

All implement one `Strategy` interface and are selectable with `--strategy`:

- **`donchian`** — trend-following (long-or-flat) per [docs/recommended-model.md](docs/recommended-model.md):
  `close > EMA(200)` regime filter, 20-day breakout entry, 10-day breakdown trailing
  exit, ATR(14)×2 stop. The honest baseline.
- **`donchian_futures`** — same but **long AND short** (for the leverage/futures engine).
- **`mean_reversion`** — Bollinger-band reversion (buy lower band / short upper band,
  exit at the mean) for **ranging** markets.
- **`regime`** (default for `serve`) — **regime-switch meta-strategy**: a Choppiness-Index
  detector runs trend-following in trending markets and mean-reversion in ranging ones —
  the evidence-based "right tool per regime" conclusion from the research below.
- **`confluence`** — RSI-pullback (spec §4.4), kept for comparison.

> Research note (deep multi-source study, 2026): for a ~$1000 account the durable,
> capturable approaches are systematic **trend-following**, **mean-reversion in confirmed
> ranges**, and a carefully-sized **basis/funding carry** — NOT MEV/HFT/stat-arb (capital-
> and infrastructure-gated) and NOT tight profit-protection stops (they cut trend winners;
> A/B: trail-off +221% vs tight-trail −86% on the same data). Most retail bots lose to
> costs; >80% of day traders lose in 6 months (Taiwan study).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then edit .env (only needed for paper/live)
```

## Usage (Phase 2 — backtest)

```powershell
# Full history backtest with equity curve (saved to data_store/backtests/)
python main.py --mode backtest --symbol BTC/USDT

# Across bull / bear / sideways regimes (the decision gate)
python main.py --mode backtest --symbol BTC/USDT --regime all

# Out-of-sample split
python main.py --mode backtest --symbol BTC/USDT --split 2023-01-01

# Compare the alternative strategy
python main.py --mode backtest --strategy confluence --timeframe 1h
```

Public OHLCV needs no API keys, so backtests run out of the box. Data is cached to
`data_store/ohlcv/` so repeated runs make no network calls.

## Paper trading with a fake balance + live monitor (no keys)

No API keys needed — these use a simulated balance (default 400 USDT) and real public
prices.

```powershell
# Watch it trade instantly: fast-forward history through a live dashboard
python main.py --mode replay --symbol BTC/USDT --speed 40
python main.py --mode replay --timeframe 1h --start 2024-01-01 --speed 60

# Forward paper trading on REAL live prices, refreshing dashboard (Ctrl+C to stop)
python main.py --mode simulate --symbol BTC/USDT --timeframe 1h --poll 5

# Print the saved simulation state once
python main.py --mode status

# Plain text instead of the live dashboard (for logs / headless)
python main.py --mode simulate --no-watch --poll 10
```

The live dashboard shows equity, cash, position (FLAT/LONG), entry/stop, unrealized PnL,
the last signal, pending orders, and recent activity. `--capital N` sets the fake
balance; `--reset` starts the simulation fresh; state persists to `data_store/sim_state.json`
so a restart resumes. For a daily strategy, use `--timeframe 1h` (or `15m`) to see more
action while watching.

## Live web dashboard (browser)

A small, fast, self-contained web page (Python stdlib only) to watch profit in real time:

```powershell
# Live monitoring on real prices (opens your browser to http://127.0.0.1:8000)
python main.py --mode serve --symbol BTC/USDT --timeframe 1h --capital 1000

# Multi-coin + leverage, live, with a 30-minute session timer
python main.py --mode serve --source live --symbols "BTC/USDT,ETH/USDT,SOL/USDT" --leverage 5 --timeframe 1m --capital 1000 --duration 30

# Lively demo: drive the dashboard with accelerated replay
python main.py --mode serve --source replay --speed 6 --capital 1000 --start 2023-01-01
```

Shows total equity/profit, % return, an equity-curve chart, **per-coin positions**,
trades (W/L), fees paid, **leverage + liquidations**, last signal, an optional **session
timer**, and a live activity log — auto-refreshing every second.

Flags: `--symbols "A,B,C"` (multi-coin), `--leverage N` (1=spot; >1 adds liquidation
risk), `--risk N` (% risk per trade), `--duration M` (session minutes), `--port N`,
`--no-open`.

### Perpetual futures (world-class leverage sim)

`serve` defaults to the **long/short** `donchian_futures` strategy and a futures margin
engine (`src/execution/futures.py`, `PortfolioEngine`) modelled on Binance USDⓈ-M:

- **Long & short** — trades both directions (shorts profit in downtrends).
- **Tiered maintenance margin** → accurate **liquidation price** (not naive `1−1/L`);
  liquidation triggers when the position's margin ratio hits 100% (margin lost + liq fee).
- **Funding rate** (perpetual) — longs pay shorts every 8h (`--funding`, per-8h rate).
- **Mark-price liquidation**, **ROE %**, **margin ratio**, **liq price** shown per coin.

```powershell
python main.py --mode serve --symbols "BTC/USDT,ETH/USDT,SOL/USDT" --leverage 10 --funding 0.0001 --capital 1000
```

> ⚠️ Leverage amplifies **both** profit and loss; at 10× a ~10% adverse move liquidates
> the position (margin lost). Funding + fees + liquidations are real drags. Paper money
> only — and most retail traders lose money using leverage. Spot (`--leverage 1
> --strategy donchian`) remains the project's safe default.

> Exchange-agnostic: built on CCXT, so Binance is not required — set `EXCHANGE` in `.env`
> (e.g. `kraken`, `coinbase`) to use public data from another exchange.

## Real trading (Phase 3 — needs your keys)

```powershell
# 1) Put testnet keys in .env (BINANCE_TESTNET=true, MODE=paper)
# 2) One iteration (ideal for Windows Task Scheduler, daily):
python main.py --mode paper --once
# 3) Or a scheduled loop:
python main.py --mode paper
# Live (real money) — only after weeks of testnet, requires explicit opt-in:
python main.py --mode live --once --i-understand-live
```

Real execution is **spot, long-or-flat** (no leverage/shorting with real money — that
stays simulation-only). Orders pass Strategy → `RiskManager.approve_*` (single-use token)
→ `Broker` (refuses any unapproved order; floors to lot size; skips below min-notional).
A mode/testnet interlock refuses to start if keys and `BINANCE_TESTNET` disagree.

## Validate before you trust it (most important)

```powershell
# Full metrics for the long/short + regime strategy (uses the live engine):
python main.py --mode backtest --strategy regime --leverage 3 --start 2023-01-01
#   -> total return, CAGR, max drawdown, Sharpe/Sortino, win rate, profit factor,
#      liquidations, funding, AND a rolling per-period breakdown (is the edge consistent?)

# Walk-forward parameter search (grid on TRAIN, validated OUT-OF-SAMPLE):
python main.py --mode optimize --symbol BTC/USDT --strategy regime
#   -> if out-of-sample is much worse than train, the params are overfit. Don't trust them.
```

## Safety & monitoring (live modes)

- **Max-drawdown breaker** (`MAX_DRAWDOWN_PCT`) and daily-loss halt auto-trip the kill switch.
- **Exchange reconciliation** clears stale positions if state and the exchange disagree.
- **Total-exposure cap** (`MAX_TOTAL_EXPOSURE`) limits correlated risk across coins.
- **Telegram alerts** on every trade and kill — set `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID`
  in `.env` (optional; silent no-op if unset).

## Tests

```powershell
pytest
```

60 tests: indicators (hand-computed), risk sizing/limits/kill-switch, deterministic
strategy signals (incl. regime/mean-reversion), futures margin/liquidation/trailing, the
order-approval token gate, and broker safety (dry-run, forged-token rejection).

## Status

- ✅ Phase 0: scaffold, config, logging
- ✅ Phase 1: data fetcher + indicators (+ tests)
- ✅ Phase 2: strategies, risk sizing, event-driven backtester (+ tests)
- ✅ Keyless paper trading: `replay` + `simulate` + `serve` (web dashboard) modes
- ✅ Phase 3: real spot execution — `broker.py` (CCXT, token-gated), `live.py` runner
  (`--mode paper`/`live`, `--once` or scheduled). **Add your Binance keys to `.env` to run.**
- ⏳ Phase 4: small live — flip `.env` to live keys + `--i-understand-live` after testnet weeks
- ⏳ Phase 4: small live — needs your live keys

## Project layout

See [docs/trading-bot-spec.md §3](docs/trading-bot-spec.md). Key modules:
`config/settings.py`, `src/indicators/`, `src/strategy/`, `src/risk/`,
`src/backtest/`, `src/data/`, `tests/`.
