"""Market-making strategy and its trade-by-trade backtest.

Instead of crossing the spread (and paying the taker fee), we rest a buy order
at the best bid and a sell order at the best ask and wait for other people to
trade against us. Makers pay no fee. The catch is "adverse selection": the
orders most likely to hit us are from people who know the price is about to
move against us.

The backtest replays every real trade in a market. A resting order of ours is
filled when a real trade happened at our price or worse. What the trade record
can NOT tell us is how many other orders were queued ahead of ours, so that is
a setting (`queue_usd`) and results should be read across a range of values.

Prices are always expressed for outcome 0. "Selling" outcome 0 at price A is,
on Polymarket, buying outcome 1 at 1 - A; the money works out the same.
"""
from __future__ import annotations

import bisect
import math
from collections import deque
from dataclasses import dataclass, field

from .strategy import taker_fee

EPS = 1e-6


@dataclass
class MakerConfig:
    quote_usd: float = 25.0        # size of each resting order
    max_inventory_usd: float = 100.0  # stop adding to a side beyond this
    queue_usd: float = 1000.0      # assumed $ of other orders ahead of ours
    through_only: bool = False     # True = worst case: fill only when price trades THROUGH us
    latency_s: float = 1.0         # delay before a new/changed/cancelled quote takes effect
    min_price: float = 0.10
    max_price: float = 0.90
    max_spread: float = 0.05       # don't quote into a very wide, broken book
    improve: bool = False          # if spread >= 2 ticks, quote one tick inside it
    jump: float = 0.03             # pull quotes after a move this big...
    jump_window_s: int = 60        # ...within this long...
    pause_s: int = 120             # ...for this long
    phase: str = "all"             # "pregame", "ingame", "other" (non-sports) or "all"
    pregame_buffer_s: int = 300    # stop quoting this long before a game starts


@dataclass
class Order:
    price: float
    remaining: float   # shares
    queue: float       # shares still ahead of us at this price


@dataclass
class Result:
    label: str = ""
    fills: int = 0
    volume_usd: float = 0.0
    pnl: float = 0.0                # final, after liquidating leftover inventory
    liquidation_cost: float = 0.0   # spread + taker fee paid to flatten at the end
    rebate_est: float = 0.0         # NOT included in pnl; unverified
    markout_60: float = 0.0         # sum over fills of shares * (move in our favour after 60s)
    markout_shares: float = 0.0
    peak_inventory_usd: float = 0.0
    trades_seen: int = 0


def normalize(rows: list[tuple], tick: float) -> list[tuple]:
    """Trade rows -> (ts, kind, price0, size).

    kind "S": the taker sold outcome 0 (a trade against resting bids).
    kind "B": the taker bought outcome 0 (a trade against resting asks).
    Buying outcome 1 at q is the same as selling outcome 0 at 1 - q.
    """
    out = []
    for ts, side, idx, price, size in rows:
        if idx == 0:
            out.append((ts, "B" if side == "BUY" else "S", price, size))
        else:
            out.append((ts, "S" if side == "BUY" else "B", 1 - price, size))
    return out


def simulate(events: list[tuple], cfg: MakerConfig, tick: float = 0.01,
             game_start: float | None = None, fee_rate: float = 0.0,
             fee_exp: float = 1.0, rebate_rate: float = 0.0, label: str = "") -> Result:
    res = Result(label=label, trades_seen=len(events))
    bid = ask = None                 # our estimate of the best bid/ask from the tape
    my_bid: Order | None = None
    my_ask: Order | None = None
    pending: deque = deque()         # (activate_ts, bid_price|None, ask_price|None)
    cash = inv = 0.0
    paused_until = 0.0
    recent: deque = deque()          # (ts, mid) for jump detection
    mids_t: list[float] = []
    mids_p: list[float] = []
    fills: list[tuple] = []          # (ts, +1 buy / -1 sell, price, shares)

    def in_phase(ts: float) -> bool:
        if cfg.phase == "all":
            return True
        if game_start is None:
            return cfg.phase == "other"
        if cfg.phase == "pregame":
            return ts < game_start - cfg.pregame_buffer_s
        return cfg.phase == "ingame" and ts >= game_start

    def liquidate() -> None:
        """Flatten inventory by crossing the spread and paying the taker fee."""
        nonlocal cash, inv
        if abs(inv) < 1e-9 or bid is None or ask is None:
            return
        px = bid if inv > 0 else ask
        mid = (bid + ask) / 2
        fee = taker_fee(abs(inv), px, fee_rate, fee_exp)
        cash += inv * px - fee
        res.liquidation_cost += abs(inv) * abs(mid - px) + fee
        inv = 0.0

    was_in_phase = False
    for ts, kind, p, size in events:
        active = in_phase(ts)
        if was_in_phase and not active:
            # We know the schedule, so quotes are pulled and inventory
            # flattened before the phase ends (e.g. before the game starts).
            my_bid = my_ask = None
            pending.clear()
            liquidate()
        was_in_phase = active

        # 1. quotes we sent earlier come into effect
        target = None
        while pending and pending[0][0] <= ts:
            target = pending.popleft()
        if target:
            _, tb, ta = target
            if tb is None:
                my_bid = None
            elif my_bid is None or abs(my_bid.price - tb) > EPS:
                my_bid = Order(tb, cfg.quote_usd / tb, cfg.queue_usd / tb)
            if ta is None:
                my_ask = None
            elif my_ask is None or abs(my_ask.price - ta) > EPS:
                my_ask = Order(ta, cfg.quote_usd / (1 - ta), cfg.queue_usd / (1 - ta))

        # 2. would this real trade have filled us?
        order, sign = (my_bid, 1) if kind == "S" else (my_ask, -1)
        if order:
            through = (p < order.price - EPS) if kind == "S" else (p > order.price + EPS)
            fill = 0.0
            if through:
                fill = min(order.remaining, size)
            elif abs(p - order.price) <= 1e-4 and not cfg.through_only:
                ahead = min(order.queue, size)
                order.queue -= ahead
                fill = min(order.remaining, size - ahead)
            if fill > 1e-9:
                cash -= sign * order.price * fill
                inv += sign * fill
                order.remaining -= fill
                fills.append((ts, sign, order.price, fill))
                res.fills += 1
                cost = order.price if sign > 0 else 1 - order.price
                res.volume_usd += cost * fill
                res.rebate_est += rebate_rate * taker_fee(fill, order.price, fee_rate, fee_exp)
                if order.remaining * cost < 1.0:
                    if sign > 0:
                        my_bid = None
                    else:
                        my_ask = None

        # 3. update our view of the book from the trade
        if kind == "B":
            ask = math.ceil((p - EPS) / tick) * tick
            if bid is None or bid >= ask - EPS:
                bid = ask - tick
        else:
            bid = math.floor((p + EPS) / tick) * tick
            if ask is None or ask <= bid + EPS:
                ask = bid + tick
        mid = (bid + ask) / 2
        mids_t.append(ts)
        mids_p.append(mid)
        recent.append((ts, mid))
        while recent[0][0] < ts - cfg.jump_window_s:
            recent.popleft()
        if abs(mid - recent[0][1]) >= cfg.jump:
            paused_until = ts + cfg.pause_s
        res.peak_inventory_usd = max(res.peak_inventory_usd,
                                     inv * mid if inv > 0 else -inv * (1 - mid))

        # 4. decide what we want resting next (takes effect after latency)
        tb = ta = None
        ok = (active and ts >= paused_until and cfg.min_price <= mid <= cfg.max_price
              and ask - bid <= cfg.max_spread + EPS)
        if ok:
            tb, ta = bid, ask
            if cfg.improve and ask - bid >= 2 * tick - EPS:
                tb, ta = bid + tick, ask - tick
            if inv * mid >= cfg.max_inventory_usd:
                tb = None
            if -inv * (1 - mid) >= cfg.max_inventory_usd:
                ta = None
        pending.append((ts + cfg.latency_s, tb, ta))

    liquidate()
    res.pnl = cash
    for ts, sign, price, shares in fills:
        i = bisect.bisect_right(mids_t, ts + 60) - 1
        res.markout_60 += sign * (mids_p[i] - price) * shares
        res.markout_shares += shares
    return res


def combine(results: list[Result], label: str) -> dict:
    tot = Result(label=label)
    for r in results:
        tot.fills += r.fills
        tot.volume_usd += r.volume_usd
        tot.pnl += r.pnl
        tot.liquidation_cost += r.liquidation_cost
        tot.rebate_est += r.rebate_est
        tot.markout_60 += r.markout_60
        tot.markout_shares += r.markout_shares
    traded = [r for r in results if r.fills]
    return {
        "label": label,
        "markets_traded": len(traded),
        "fills": tot.fills,
        "volume_usd": tot.volume_usd,
        "pnl": tot.pnl,
        "pnl_per_100_traded": 100 * tot.pnl / tot.volume_usd if tot.volume_usd else 0.0,
        "markout_60s_cents": 100 * tot.markout_60 / tot.markout_shares if tot.markout_shares else 0.0,
        "liquidation_cost": tot.liquidation_cost,
        "rebate_est": tot.rebate_est,
        "markets_profitable": sum(1 for r in traded if r.pnl > 0),
        "worst_market": min((r.pnl for r in traded), default=0.0),
        "best_market": max((r.pnl for r in traded), default=0.0),
        "peak_inventory_usd": max((r.peak_inventory_usd for r in traded), default=0.0),
    }
