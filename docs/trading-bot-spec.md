# Crypto Trading Bot: Project Specification

A practical, phased plan for building a personal crypto trading bot for Binance,
designed to be implemented step by step in Claude Code.

---

## 0. Read this first (risk and scope)

This is a personal, educational project. Keep these facts in front of you the whole time:

- Trading is high risk. You can lose all of the capital you allocate. Build the bot only with money you can afford to lose.
- A bot does not guarantee profit. It executes a strategy with discipline and speed. If the strategy has no real edge, the bot just loses money more consistently.
- This document is not financial advice. The strategy below is a hypothesis to be tested, not a recommendation to make money.
- With a small account, fees and minimum order sizes dominate. Trade rarely and only liquid pairs.
- Confirm that Binance is available and legal in your jurisdiction, complete KYC, and handle taxes correctly.

**Non-goals (deliberately out of scope for v1):**

- Scanning hundreds of coins to "pick the winner". This is much harder than trading 1 to 3 known pairs and is left for later.
- Leverage, futures, or margin. v1 is spot only.
- Promising or targeting a fixed return.

---

## 1. Goal and success criteria

**Goal:** a spot trading bot for Binance that fetches market data, runs a rule based
strategy, manages risk strictly, and can run in three modes: backtest, paper (Binance testnet), and small live.

**Success is defined by system correctness, not profit:**

- Connects to Binance (testnet and live) reliably.
- Fetches OHLCV data and computes indicators correctly (covered by unit tests).
- Strategy produces deterministic signals from a given dataset.
- Risk manager never exceeds the configured limits (position size, max open positions, max daily loss).
- Orders respect Binance filters (minimum notional, lot size, price precision).
- Dry run mode never sends real orders.
- Every trade and error is logged.
- The kill switch halts trading on demand and automatically on the daily loss limit.

"Makes money" is explicitly NOT a success criterion. That depends on market conditions and whether the strategy has an edge, which is uncertain.

---

## 2. Tech stack

- Language: Python 3.11+
- Exchange access: CCXT (unified API, portable across exchanges, supports Binance sandbox)
- Data handling: pandas, numpy
- Indicators: `pandas-ta` (or `ta` as a fallback if there are version conflicts with numpy/pandas)
- Config and secrets: `python-dotenv`
- Logging: `loguru`
- Scheduling: `apscheduler` (or `schedule`)
- Plotting (equity curve, backtest): `matplotlib`
- Tests: `pytest`

`requirements.txt`:

```
ccxt
pandas
numpy
pandas-ta
python-dotenv
loguru
apscheduler
matplotlib
pytest
```

Note: if `pandas-ta` fails to import with a newer numpy, switch to the `ta` library or pin compatible versions. Keep indicator logic isolated so swapping the library touches one module only.

---

## 3. Project structure

```
trading-bot/
  .env.example
  .gitignore
  requirements.txt
  README.md
  config/
    settings.py          # all parameters in one place
  src/
    data/
      fetcher.py         # OHLCV + account data via CCXT
    indicators/
      indicators.py      # EMA, RSI, ATR, etc.
    strategy/
      base.py            # Strategy interface
      confluence.py      # baseline strategy
    risk/
      manager.py         # position sizing, limits, kill switch
    execution/
      broker.py          # CCXT wrapper: dry-run, testnet, live
    backtest/
      backtester.py      # vectorized backtest with fees + slippage
    runner/
      live.py            # main loop for paper and live
    utils/
      logger.py
      state.py           # persist open positions, daily PnL, kill state
  data_store/
    trades.csv           # trade log (or sqlite later)
  tests/
    test_indicators.py
    test_risk.py
    test_strategy.py
```

---

## 4. Module specifications

### 4.1 config/settings.py

A single source of truth. Read secrets from environment, keep tunables here.

Key settings:

- `MODE`: one of `backtest`, `paper`, `live`
- `EXCHANGE`: `binance`
- `PAIRS`: e.g. `["BTC/USDT", "ETH/USDT"]` (start with one, add the second later)
- `TIMEFRAME`: e.g. `1h` (slower timeframes mean fewer trades and lower fee drag)
- Indicator params: `EMA_TREND=200`, `RSI_PERIOD=14`, `RSI_BUY=35`, `RSI_EXIT=55`, `ATR_PERIOD=14`, `ATR_STOP_MULT=2.0`, `REWARD_RISK=1.5`
- Risk params (see section 5): `RISK_PER_TRADE_PCT`, `MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS_PCT`
- `LIVE_CAPITAL_CAP`: a hard cap on how much real money the bot may use, separate from your total balance

### 4.2 src/data/fetcher.py

- Fetch historical OHLCV for a pair and timeframe via CCXT.
- Fetch current account balance and open orders.
- Return clean pandas DataFrames with columns: `timestamp, open, high, low, close, volume`.
- Handle rate limits and transient network errors with retry and backoff.

### 4.3 src/indicators/indicators.py

Pure functions, input a price DataFrame, output the indicator series:

- `ema(df, period)`
- `rsi(df, period)`
- `atr(df, period)`

No trading logic here. This module must be unit tested against known values.

### 4.4 src/strategy/base.py and confluence.py

`base.py` defines the interface every strategy implements:

```python
class Strategy:
    def generate_signal(self, df) -> dict:
        """
        Return e.g. {"action": "buy"|"sell"|"hold", "reason": str}
        Must be deterministic for a given df.
        """
        raise NotImplementedError
```

`confluence.py` is the baseline. Entry and exit rules:

- Trend filter: only consider long entries when `close > EMA(200)`. The bot does not short in v1.
- Entry (long): when in an uptrend and `RSI < RSI_BUY` (a pullback inside a trend).
- Exit: when `RSI > RSI_EXIT`, or stop loss hit, or take profit hit.
- Stop loss: `entry_price - ATR_STOP_MULT * ATR`.
- Take profit: `entry_price + REWARD_RISK * (entry_price - stop_price)`.

This uses three non redundant views (trend, momentum, volatility) instead of stacking many overlapping indicators. Treat the parameters as starting points to be tested, not as tuned truth.

### 4.5 src/risk/manager.py

The risk manager is the most important safety component. See section 5 for the rules. It must be impossible for the rest of the system to bypass it.

### 4.6 src/execution/broker.py

A thin wrapper over CCXT with three behaviours controlled by `MODE`:

- `dry-run` (used inside backtest and optionally paper): never calls the network, simulates fills.
- `testnet`: calls Binance Spot Testnet with testnet keys.
- `live`: calls real Binance with live keys.

Responsibilities:

- Round order quantity to the symbol `LOT_SIZE` step.
- Reject any order below `MIN_NOTIONAL`.
- Round price to allowed precision for limit orders.
- Return a normalized fill result (filled qty, average price, fee).

A single boolean must gate real order placement. In dry-run and backtest it is always false.

### 4.7 src/backtest/backtester.py

A vectorized backtest over historical OHLCV:

- Apply the strategy bar by bar.
- Model fees on every entry and exit (default round trip ~0.2%, configurable).
- Model simple slippage (for example a few basis points on market fills).
- Track equity over time.
- Output metrics (section 6) and save an equity curve plot.

### 4.8 src/runner/live.py

The main loop for paper and live modes:

1. On schedule (aligned to the timeframe), fetch latest data.
2. Compute indicators, ask the strategy for a signal.
3. Pass any intended trade to the risk manager for sizing and approval.
4. If approved, place the order via the broker.
5. Update state (open positions, daily PnL), log everything.
6. Check the kill switch before every action.

### 4.9 src/utils/

- `logger.py`: structured logs to file and console.
- `state.py`: persist open positions, realized daily PnL, and kill switch state so a restart does not lose track.

---

## 5. Risk management rules (concrete)

These are hard rules, enforced in `risk/manager.py`. With a small account they matter more than the strategy.

- **Risk per trade:** `RISK_PER_TRADE_PCT = 1.0`. On a 400 USDT account this is 4 USDT of risk per trade.
- **Position sizing:** `position_value = (capital * risk_pct) / stop_distance_pct`, then cap by available balance and by `LIVE_CAPITAL_CAP`. If the result is below `MIN_NOTIONAL`, skip the trade rather than forcing it.
- **Max open positions:** `MAX_OPEN_POSITIONS = 2`.
- **Max daily loss:** `MAX_DAILY_LOSS_PCT = 5`. If realized loss for the day reaches this, halt all new entries until the next day.
- **Kill switch:** manual flag plus automatic trigger on the daily loss limit or on N consecutive errors. When active, the bot manages existing exits only and opens nothing new.
- **One source of truth for capital:** the bot never uses more than `LIVE_CAPITAL_CAP`, even if the account holds more.

Honest note for under 500 USDT: a 1 percent risk per trade plus the 5 USDT minimum notional plus round trip fees means you can realistically hold only 1 to 2 positions and should trade infrequently. At this size, results are dominated by noise and fees. The point of v1 is to build a correct, safe system, not to grow the account quickly.

---

## 6. Backtesting requirements

Before any live money, the strategy must be backtested honestly.

Metrics to report:

- Total return percent and CAGR
- Maximum drawdown percent
- Sharpe or Sortino ratio
- Win rate and profit factor
- Number of trades and average trade
- Time in market (exposure)

Rules to avoid fooling yourself:

- Include fees and slippage. A strategy that is profitable before fees and unprofitable after is unprofitable.
- Test across different regimes: a bull period, a bear period, and a sideways period.
- Use out of sample data: tune on one period, then test on a later period you did not look at.
- Be suspicious of great results. The more parameters you tune, the more likely you are overfitting.

Decision gate: proceed to paper trading only if behaviour is sane and results are not clearly negative across regimes. This is a sanity check, not a profit guarantee.

---

## 7. Phased build plan

Build in this order. Do not skip ahead. Each phase has a concrete deliverable.

**Phase 0: Setup**
Create the Binance account, complete KYC, generate Spot Testnet keys, set up the repo, install dependencies, create `.env` from `.env.example`, wire up logging.
Deliverable: the project runs, connects to testnet, and prints the testnet account balance.

**Phase 1: Data and indicators**
Implement `fetcher.py` and `indicators.py`. Pull historical OHLCV for one pair and compute EMA, RSI, ATR.
Deliverable: correct data and indicators, with passing unit tests.

**Phase 2: Backtester and baseline strategy**
Implement the `Strategy` interface, the `ConfluenceStrategy`, and the vectorized backtester with fees and slippage. Produce a metrics report and equity curve.
Deliverable: a backtest report across at least three market regimes. Apply the decision gate.

**Phase 3: Paper trading on testnet**
Wire strategy, risk manager, and broker together in testnet mode. Run the loop on schedule for 2 to 4 weeks. Log simulated trades.
Deliverable: a paper trading log, no crashes, all risk limits respected.

**Phase 4: Small live**
Switch to live keys. Set `LIVE_CAPITAL_CAP` to a small slice, for example 50 to 100 USDT of your under 500 budget. Keep strict limits and monitor closely.
Deliverable: the bot runs safely live and you can explain every trade it made.

**Phase 5: Iterate (later, optional)**
Only after the above is solid: add more strategies behind the same interface, compare them, optionally build a signal aggregator. Add websockets for realtime data, Telegram alerts, a simple dashboard, and careful parameter studies. This is where your "analyze more things" idea belongs, in a controlled and tested way. News and sentiment context belong here too, used as a risk filter rather than a trade trigger. See section 10.

---

## 8. Security checklist (do this yourself, never share keys)

- API key permissions: enable Reading and Spot Trading only. Disable withdrawals. There is no reason a trading bot needs withdrawal access.
- Enable IP whitelist on the API key.
- Store keys in `.env`. Never commit them. Add `.env` to `.gitignore`.
- Consider a dedicated Binance sub account for the bot so it is isolated from your main holdings.
- Never paste real API keys into code, into a chat, or into any document. Set them up in your own environment.
- If a key is ever exposed, revoke and rotate it immediately.

`.env.example`:

```
BINANCE_API_KEY=your_key_here
BINANCE_API_SECRET=your_secret_here
BINANCE_TESTNET=true
```

---

## 9. Binance specific notes

- Spot Testnet: https://testnet.binance.vision uses its own API keys, separate from your main account. Use it for all of Phase 3.
- CCXT sandbox: call `exchange.set_sandbox_mode(True)` to route to the testnet.
- Fees: approximately 0.1 percent spot, around 0.075 percent if paying fees with BNB. Verify the current schedule in your account, since fee tiers change. Assume a round trip cost near 0.2 percent when backtesting.
- MIN_NOTIONAL: often around 5 USDT but it varies by symbol. Read the symbol filters from the exchange and respect them.
- LOT_SIZE: order quantity must be a multiple of the step size. Round down to a valid quantity.
- Rate limits: Binance uses weight based limits. Cache data, avoid unnecessary calls, and handle HTTP 429 and 418 responses with backoff.

---

## 10. News and sentiment (deferred, optional)

News genuinely moves crypto. Regulation, macro releases, exchange events, protocol exploits, and influential accounts can all cause large moves, so the instinct that news matters is correct. The hard part is turning that into a working bot, and for a retail account it is one of the most difficult paths. Treat this as a later, optional addition, not part of v1.

Why a news driven entry signal is hard at retail scale:

- Latency. Markets price in news within seconds. By the time a retail news API delivers a headline and the bot reacts, professional players with direct feeds have already moved the price. You end up chasing, buying the top or selling the bottom.
- Direction is not obvious. Good news often causes a drop ("sell the news"), and bad news sometimes bounces. Knowing that news happened does not tell you which way price goes.
- Sentiment analysis is noisy. Scoring crypto headlines for sentiment is unreliable, especially with jargon and irony. False signals are common.
- Backtesting is very hard. You need point in time historical news with accurate timestamps to avoid lookahead bias. Clean data of this kind is expensive and hard to source, so you cannot easily validate a news strategy.

Why "combine many APIs" is the wrong goal:

- More data sources do not create edge. They add cost, rate limits, failure points, and conflicting signals. This is the same trap as stacking many indicators, applied to data feeds.
- Cost. Good news and data APIs often run 50 to several hundred USD per month. On an account under 500 USD, a data subscription can exceed your trading capital, which makes no economic sense.

The realistic way to use news at this scale is as a risk filter and context, not as a trade trigger:

- Economic calendar. Keep a small list of scheduled high impact events (for example FOMC and CPI). Around these, pause new entries or reduce position size, because volatility and spreads spike. This is predictable and free.
- A few free context signals, for awareness only and never as triggers: the Fear and Greed index, a free tier crypto news feed, and exchange funding rates or open interest as crowd positioning proxies. Verify the current free tiers before relying on any of them.
- The rule: news can tell the bot when to be cautious. It must not be trusted to tell the bot what to buy.

If you later want to go further, evaluate specific providers and their current pricing, and design a proper point in time backtest before trusting any news signal. Until then, the safest contribution news makes is keeping the bot out of trouble during chaotic moments.

---

## 11. Glossary

- OHLCV: open, high, low, close, volume for each time bar.
- EMA: exponential moving average, used here as a trend filter.
- RSI: relative strength index, a momentum oscillator from 0 to 100.
- ATR: average true range, a measure of volatility used to size stops.
- Drawdown: the drop from a peak in account equity, expressed as a percent.
- Slippage: the difference between expected and actual fill price.
- Notional: order size in quote currency (for BTC/USDT, the USDT value).
- Maker or taker: whether your order adds liquidity (maker) or removes it (taker), which affects the fee.

---

## 12. First step for Claude Code

Start with Phase 0. Ask Claude Code to scaffold the folder structure above, create
`requirements.txt`, `.env.example`, `.gitignore`, and a minimal `runner/live.py` that
connects to Binance Spot Testnet via CCXT and prints the account balance. Confirm that
works before writing any strategy code.
