# Polymarket overshoot scanner & paper trader

Watches busy Polymarket markets (especially games in progress), flags prices
that jumped far more than usual and have started to drift back, and simulates
short in-and-out trades betting on the drift continuing.

**It trades pretend money only. It cannot place real orders.** See
"Backtest results" for why.

## Quick start

```bash
pip install -r requirements.txt
python -m overshoot scan --live-only        # what's stretched right now
python -m overshoot backtest --live-only    # replay the last 3 days
python -m overshoot paper --live-only       # live prices, pretend money
python -m overshoot mm-backtest             # market-maker test on real trades
python -m overshoot arb-scan                # events whose prices don't add up
python -m overshoot arb-watch --minutes 20  # ...watched live over time
python -m overshoot arb-backtest            # ...looked for in past trades
python -m overshoot btc-backtest            # 5-minute Bitcoin Up/Down strategy
python -m overshoot btc-backtest --history-days 40   # ...plus a 40-day check
python -m overshoot btc-paper               # ...live, with pretend money
python -m pytest                            # run the tests
```

Drop `--live-only` to include politics, crypto, economics and so on.
Any setting in `overshoot/config.py` can be changed from the command line:

```bash
python -m overshoot --set z_enter=6 --set stake_usd=10 backtest
```

## How it works

1. **Pick markets** - busy, liquid, tight spread, price between 10c and 90c.
2. **Measure normal movement** - how much the price usually moves in 5
   minutes, from the previous 3 hours.
3. **Spot an overshoot** - a 5-minute move of at least 4 cents *and* at least
   4x the normal size, which has already given back 20-45% of the jump.
4. **Fade it** - buy the side that got cheaper (Polymarket has no short
   selling, so betting against outcome A means buying outcome B).
5. **Get out** - take profit when 55% of the jump is undone, stop out if it
   runs 35% further, or leave after 20 minutes.

Safety limits: $25 per trade, 5 open positions, one position per event,
15-minute cooldown per market, no new trades after losing $50 in a day.

## Backtest results (11 Nov 2026) - read this before risking money

Replaying roughly 3 days of 1-minute prices for ~200 sports/esports markets,
trading only after each game started, $25 per trade:

| Settings | Trades | Win rate | Before fees | After fees |
|---|---|---|---|---|
| Default | 74 | 72% | -$29 | **-$92** |
| Bigger spikes only (6x, 6c) | 44 | 70% | -$51 | -$88 |
| Biggest spikes only (8x, 8c) | 25 | 72% | -$25 | -$45 |
| Tighter stop | 86 | 69% | -$14 | -$90 |
| Very wide stop | 93 | 77% | +$32 | -$45 |
| Bigger target, hold up to 60 min | 81 | 64% | +$70 | +$3 |
| 2-minute window | 44 | 66% | -$70 | -$108 |
| 15-minute window | 83 | 70% | -$57 | -$124 |

Eleven of twelve variations tested lost money; the one that didn't made $3 on
81 trades, which is indistinguishable from luck.

Why it loses:

- **Small wins, big losses.** About 7 in 10 trades win ~$2.50, but the losers
  average ~$10. In a live game a big jump is usually real news (a break of
  serve, a goal), and the price can gap from 51c to 14c before any stop works.
- **Fees.** Taking liquidity costs about 1.25c per share near 50c, each way.
  That is as big as the profit being targeted.

## What the numbers can't tell you

- The fee formula (`taker_fee` in `strategy.py`) is my reading of the fee
  schedule the API reports. It has **not** been checked against a real fill.
- The backtest uses 1-minute prices and assumes a 1c spread. Real fills,
  especially stops during a fast move, will usually be worse.
- Three days is a small sample, all from one week.
- All tested variations were run on the same data, so even the best row is
  flattered.

## Market-making test (11 Nov 2026)

Idea: instead of paying the taker fee, rest a $25 buy order at the best bid
and a $25 sell order at the best ask, and earn the gap when both get filled.
`mm-backtest` replays every real trade (432,439 trades, 150 markets, 3 days)
and fills our orders only when a real trade happened at our price or worse.

The trade record doesn't show how many other orders were ahead of ours in the
queue, so the test is run under four assumptions:

| When we quote | Queue ahead of us | Fills | P&L | Per $100 traded |
|---|---|---|---|---|
| Before games | none (best case) | 13,487 | +$441 | +$0.30 |
| Before games | $1,000 | 2,275 | -$602 | -$1.53 |
| Before games | $10,000 | 1,525 | -$641 | -$2.69 |
| Before games | worst case | 1,340 | -$642 | -$3.32 |
| During games | none (best case) | 11,339 | -$2,766 | -$1.84 |
| During games | $1,000 | 4,373 | -$3,284 | -$4.69 |
| Non-sports | none (best case) | 6,410 | -$39 | -$0.04 |
| Non-sports | $1,000 | 2,066 | -$518 | -$1.34 |

What this says:

- **It only works if we are first in line**, which we would not be. A spot
  check of 15 open markets found a median of roughly $1,000-$2,500 already
  resting at the best price (ranging from under $100 to over $100,000).
- **With a queue, we mostly get filled when the price is about to move
  against us.** The orders that reach the back of the line are the ones that
  clear out the whole price level.
- **During games it loses under every assumption**, even the best case.
- Seven variations (smaller inventory, quoting inside the spread, pulling
  quotes sooner, faster/slower reaction time, mid-range prices only, stopping an hour before games) were also
  tried before games and on non-sports markets. All of them lost money with a
  $1,000 queue.

Not included: Polymarket's fee schedule mentions a maker rebate (15-25% of
taker fees). If it works the way I guess, it would have added roughly $140
to the "before games, $1,000 queue" row - not enough to turn it positive. I
have not verified how rebates are actually paid.

Limits of this test: three days of data; the best bid/ask are inferred from
trades rather than recorded order books; leftover inventory is sold at the
end at the worse price plus the taker fee; our own orders are assumed not to
change what other traders did.

Settings live in `MakerConfig` (`overshoot/maker.py`) and can be overridden:

```bash
python -m overshoot mm-backtest --mm max_inventory_usd=25 --mm improve=true
```

## Price-sum test (11 Nov 2026)

Idea: in an event where exactly one outcome wins, one YES share of every
outcome pays exactly $1 (and one NO share of every outcome pays one dollar
less than the number of outcomes). If a whole basket costs less than it pays,
fees included, the difference is profit whoever wins.

**Live, against real order books** (`arb-watch`, 20 minutes, 59 events,
89 checks): 5 gaps appeared, in 2 events. The largest was worth $0.49 in
total; the others $0.07-$0.10. Each lasted 1.5-3.5 minutes.

**Past trades** (`arb-backtest`, 3 days, 48 events, 51,567 trades):

| Every leg traded within | Profitable moments | Total profit available |
|---|---|---|
| 30 seconds | 91 | $30.69 |
| 60 seconds | 96 | $31.90 |
| 5 minutes | 107 | $38.06 |
| 15 minutes | 118 | $41.48 |

The largest single one was $3.38, and most lasted under 5 seconds.

What this says: real gaps do exist, but they are worth cents, not dollars -
roughly $10 a day across every event combined, as an upper limit, before
counting the ones that were not really there.

Limits: the backtest infers prices from trades in different seconds, so its
largest "gaps" (20-30c during football matches) are almost certainly prices
from before and after a goal, not something that could be bought together.
It only covers events where every outcome is still open. Profit is only paid
when the event resolves, which can be months away. I have not checked each
event's rules by hand to confirm the listed outcomes really cover every
possibility - an event that can end with no listed outcome winning would turn
a "locked-in" profit into a loss.

## 5-minute Bitcoin Up/Down test (11 Nov 2026)

Each market covers one 5-minute window and pays $1 on "Up" if Chainlink's
60-second-average Bitcoin price at the end is at or above the one at the
start. A new market opens every 5 minutes. Taker fee: about 1.75c per share
at a 50c price (3.5% of the stake).

Data: 25,894 settled windows over 90 days, Binance second-by-second Bitcoin
prices, and every Polymarket trade for 40 days (17 million trades). The last
4 days were set aside and not looked at until the strategy was fixed.

**What predicts the next 5 minutes (24,742 windows before the test period)**

| Signal | "Up" rate when signal is strongly down | ...strongly up |
|---|---|---|
| 24-hour trend | 51% | 50% |
| 4-hour trend | 52% | 49% |
| 1-hour trend | 53% | 50% |
| 15-minute trend | 50% | 51% |
| Previous 5-minute window | 48% | 54% |
| Move in the last minute before the window | 37% | 63% |

Broader trends carry no usable information. The last-minute move does -
because the start price is a 60-second average, a late move gives one side a
head start - but the market's opening price already reflects it.

**The strategy.** During a window, estimate the chance of "Up" from the
market's own price plus where Bitcoin is now relative to the start, and buy
whichever side is cheaper than the estimate by 5c or more after fees, between
2 and 4 minutes in. Hold to settlement. $25 per trade. These settings were
chosen on the 6 days before the test period.

**Result on the untouched last 4 days (1,152 windows)**

| | Trades | Win rate | P&L | Significance |
|---|---|---|---|---|
| Strategy, realistic (order arrives 1s+ later) | 154 | 46% | +$267 | 0.4 |
| Strategy, if we were the fastest trader | 260 | 57% | +$1,162 | 1.6 |
| Strategy with trends added | 190 | 45% | -$106 | -0.1 |
| Simple: follow the last-minute move | 1,148 | 63% | +$537 | 0.8 |
| Simple: follow the 15-minute trend | 1,143 | 50% | -$2,487 | -2.9 |
| Simple: follow the 1-hour trend | 1,142 | 50% | -$2,098 | -2.4 |
| Simple: follow the 4-hour trend | 1,147 | 47% | -$3,565 | -4.1 |
| Simple: follow the 24-hour trend | 1,142 | 50% | -$1,877 | -2.1 |

"Significance" is the average profit divided by its margin of error; below
about 2 a result cannot be told apart from luck.

**Same settings over 40 days** (model refitted each day on the previous 6)

| | Period | Trades | P&L | Significance |
|---|---|---|---|---|
| Realistic | Earlier 25 days (never used for tuning) | 193 | -$1,293 | -3.2 |
| Realistic | 6 tuning days | 174 | +$421 | 0.9 |
| Realistic | Last 4 days | 118 | +$832 | 1.2 |
| Realistic | **All 40 days** | 485 | **-$40** | 0.0 |
| If fastest | All 40 days | 1,036 | +$4,309 | 3.4 |
| Follow 1-hour trend | All 40 days | 11,439 | -$13,958 | -4.9 |
| Follow 24-hour trend | All 40 days | 11,443 | -$19,987 | -7.0 |

What this says:

- **Trend-following loses, clearly.** Trends don't predict the next five
  minutes, so you pay the fee and spread on a coin flip.
- **The strategy was profitable in the last few days but not over 40 days**,
  where it nets to zero. The recent profit is within the range of luck.
- **There is a real edge, but it belongs to the fastest trader.** Buying at
  the same instant as the first real buyer is profitable in every period;
  arriving one second later is not. Winning that race needs co-located
  servers and a live Chainlink feed, not a better trend rule.

Limits: Binance prices stand in for the Chainlink feed that actually settles
the market. Fills assume $25 was available at the price a real trader paid.
The fee formula is still my unverified reading. The "if fastest" row is an
upper bound, not something achievable from a home connection.

## Paper trading the Bitcoin strategy

`python -m overshoot btc-paper` follows the live 5-minute Bitcoin markets and
makes pretend trades by the same rules as the backtest. It sends no orders and
needs no account. Each settled trade is appended to `btc_paper_trades.csv`.

- Expect a trade in roughly 1 window in 8, so several per hour.
- Run it for days, not minutes: at $25 a trade, results swing by about $25
  per trade, so fewer than a few hundred trades says very little.
- The backtest's 40-day result was about break-even. A paper run that agrees
  with that is the system working as tested, not a fault.

Useful options: `--minutes 120` to stop after two hours, `--btc stake_usd=5`
for smaller pretend bets.

## Layout

- `overshoot/btc_live.py` - live paper trading of the Bitcoin strategy
- `overshoot/btc_weights.json` - the fitted estimate used by `btc-paper`

- `overshoot/btc5m.py` - data for the 5-minute Bitcoin markets
- `overshoot/btc_strategy.py` - the Bitcoin strategy and its backtest
- `overshoot/arb.py` - price-sum check, live and on past trades
- `overshoot/maker.py` - market-making simulator
- `overshoot/config.py` - every setting, with comments
- `overshoot/api.py` - read-only Polymarket data (no account needed)
- `overshoot/strategy.py` - spike detector, fees, simulated account
- `overshoot/cli.py` - the `scan`, `backtest`, `paper` commands
- `tests/` - unit tests for the detector and trade logic
