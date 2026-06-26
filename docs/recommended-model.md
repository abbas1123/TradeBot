# Recommended Trading Model (Best Realistic Odds)

You asked for the most profitable model. This is the honest version of that.

---

## 1. About "the most profitable model"

There is no model you can write down in advance that guarantees the most profit. If
such a model existed and could simply be written out, everyone would run it, and the
moment they did it would stop working. Markets adapt. Any edge that becomes widely known
gets traded away.

So instead of a fantasy "best profit" model, this document gives the model with the best
realistic odds for your situation: small account, Binance spot, beginner, fully automated.
That model is trend following. It is not a money printer. It is the most defensible
systematic approach, and it is honest about how it behaves.

---

## 2. The recommended model: trend following (long or flat)

The bot is always in one of two states: holding BTC because a confirmed uptrend exists, or
holding USDT (cash) because it does not. It never shorts in this version.

Trend following does not try to predict the future. It reacts to what price is already
doing. It cuts losing trades quickly and lets winning trades run. This produces a positive
skew: many small losses and a few large wins. That pattern is exactly what a disciplined,
emotion free bot is good at executing, and exactly what most humans fail at by hand.

---

## 3. Why this model and not something fancier

- It is the one systematic style with broad, multi decade, cross market evidence behind it.
  Managed futures and momentum research show it has worked across many assets and eras. That
  is not a promise it keeps working, but it is the strongest track record available.
- It does not require prediction, sentiment guessing, or news APIs. Those are the parts that
  fail most often for retail.
- It is mechanical and simple, with few parameters, so there is little to overfit and little
  to break.
- It protects capital. By sitting in cash during chop and bear markets, it avoids the worst
  drawdowns instead of riding them down.
- Crypto trends hard when it trends, which is the environment trend following is built for.

---

## 4. Concrete rules

These map directly onto the architecture in the main spec (`trading-bot-spec.md`). Treat the
numbers as sensible starting points to be tested, not as tuned truth.

**Universe**
- BTC/USDT only to start. It is the most liquid pair with the tightest spread.
- Add ETH/USDT later only if the account grows. At under 500 USDT you can realistically hold
  one position.

**Timeframe**
- Daily candles (1d). A higher timeframe means fewer trades, less fee drag, and less noise.
  This matters more than anything at a small account size.

**Regime filter (are we allowed to be long at all)**
- Long bias is on only when the daily close is above the 200 period EMA. Below it, the bot
  stays in cash and ignores entry signals. This keeps you out of breakouts during bear markets.

**Entry (when to buy)**
- When long bias is on and price closes above the highest high of the prior 20 days
  (a 20 day breakout). This is the classic Donchian or Turtle entry: buy strength.

**Initial stop loss**
- Place a stop at `entry_price - 2 * ATR(14)`. Volatility sets the stop distance, not a fixed
  percent.

**Exit (when to sell)**
- Trailing exit: close the position when price closes below the lowest low of the prior 10
  days (a 10 day breakdown). This lets winners run while locking in trend reversals.
- Hard exit: also close if the daily close drops back below the 200 EMA, since the regime has
  flipped.

**Position states**
- LONG: a confirmed uptrend with a fresh breakout.
- FLAT: everything else. The bot holds USDT and waits.

**Risk (from the main spec, unchanged)**
- Risk 1 percent of capital per trade, sized from the stop distance.
- Respect MIN_NOTIONAL, so in practice one position at this account size.
- Max daily loss limit and kill switch always apply.

---

## 5. What to realistically expect

Read this carefully, because the behaviour will feel wrong if you do not expect it.

- Low win rate. Expect to be right on roughly 30 to 45 percent of trades. The profit, if any,
  comes from a small number of large winners, not from being right often.
- Frequent small losses in sideways markets. False breakouts (whipsaws) are normal. The bot
  will take a string of small losses during chop. This is the cost of the strategy, not a bug.
- Long periods doing nothing. In bear or sideways markets the bot sits in cash for weeks. This
  protects you, but it feels frustrating.
- It can lag buy and hold in a raging bull, because it exits on pullbacks and re enters late.
  In a bear it sidesteps the worst by staying in cash. It trades upside in calm markets for
  protection in bad ones.
- Lumpy, regime dependent results. There is no steady monthly gain.

On returns: I will not give you a percentage. Anyone who promises a fixed monthly return is
either lying or selling something. At under 500 USDT, fees and minimum order sizes can eat
most of even a real edge. The first job is to survive and to confirm the approach behaves
correctly, not to grow the account fast.

---

## 6. The honest alternative: DCA (accumulation)

If your real goal is to grow a small amount of crypto over one to three years with minimal
effort, dollar cost averaging often has higher expected value than active trading at this
account size. I include it because honesty requires it.

**Rules**
- Buy a fixed USDT amount of BTC (and optionally ETH) on a fixed schedule, for example every
  week, regardless of price.
- Optional tilt: buy a little more when the Fear and Greed index is in extreme fear.

**Why it can beat active trading for you**
- Almost no fee drag, because you trade rarely.
- No prediction required.
- No whipsaw losses from false signals.
- It is very hard to mess up.

**The tradeoff**
- It is directional. You are simply long the asset. If crypto stagnates for years or keeps
  falling, you stagnate or lose with it. There is no cash exit protecting you the way trend
  following has.

A reasonable path is to run DCA as the boring base, and treat the trend following bot as the
project you build, test, and learn from. Do not let the trend bot touch real money until it
has passed the validation below.

---

## 7. Before any real money

This model is a hypothesis until proven on data and on testnet. Follow the main spec:

1. Backtest it across a bull period, a bear period, and a sideways period, with fees and
   slippage included.
2. Use out of sample data. Do not trust results from the same period you tuned on.
3. Paper trade on Binance testnet for several weeks. Confirm no crashes and that every risk
   limit holds.
4. Only then go live with a tiny slice of your budget, for example 50 to 100 USDT, with the
   capital cap and kill switch active.

If the backtest is clearly negative across regimes, that is valuable information. It means the
edge is not there at your scale, and the right move is DCA, not forcing trades.

---

## 8. Final reality check

Trend following is the best honest answer to "what gives me the best odds", not a guarantee of
profit. It will have losing streaks and long flat stretches by design. Its value is that it
limits losses, removes emotion, and has a real historical basis. Build it to learn and to
survive first. Profit, if it comes, follows from discipline and cost control, not from a
secret model.
