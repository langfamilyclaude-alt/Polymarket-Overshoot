"""Live PAPER trading of the 5-minute Bitcoin Up/Down strategy.

Pretend money only: this reads public prices and never sends an order.

It follows the same rules as the backtest (see btc_strategy.py):
  * decide every 10 seconds between `t_min` and `t_max` seconds into a window
  * estimate the chance of "Up" from the market price and Bitcoin's move
  * if one side is cheaper than the estimate by `margin` after fees, wait a
    second, look at the order book again, and "buy" only if the price is
    still within our limit and enough shares are on offer
  * hold to settlement, at most one trade per window
"""
from __future__ import annotations

import csv
import json
import math
import os
import time
from datetime import datetime, timezone

from . import api, btc5m
from .btc_strategy import BtcConfig
from .strategy import taker_fee

WEIGHTS_FILE = os.path.join(os.path.dirname(__file__), "btc_weights.json")
FIELDS = ["window_start_utc", "seconds_in", "side", "price", "estimate", "shares",
          "fee", "result", "pnl", "running_total"]


def load_weights() -> list[float]:
    with open(WEIGHTS_FILE) as f:
        return json.load(f)["weights"]


def estimate_up(weights: list[float], market_mid: float, z: float) -> float:
    m = min(max(market_mid, 0.01), 0.99)
    x = weights[0] + weights[1] * math.log(m / (1 - m)) + weights[2] * z
    return 1 / (1 + math.exp(-x))


def btc_move_z(px: dict[int, float], start: int, now: int) -> float | None:
    """Bitcoin's move since the window began, in units of a typical move over
    the time remaining. px maps each second to Bitcoin's price."""
    series, last = [], None
    for sec in range(start - 1800, now):
        last = px.get(sec, last)
        series.append(last)
    if series[0] is None or series[-1] is None:
        return None       # not enough price history yet
    before = series[:1800]
    base = sum(before[-60:]) / 60
    rets = [math.log(b / a) for a, b in zip(before, before[1:])]
    mean = sum(rets) / len(rets)
    sig = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) * 1e4
    t = now - start
    if sig <= 0 or t >= 260:
        return None
    return (series[-1] / base - 1) * 1e4 / (sig * math.sqrt(260 - t))


def pick_side(p_up: float, ask_up: float, ask_dn: float, cfg: BtcConfig):
    """Returns (side, price, edge, limit) for the better side, or None."""
    best = None
    for side, price, prob in (("Up", ask_up, p_up), ("Down", ask_dn, 1 - p_up)):
        if not (cfg.min_price <= price <= cfg.max_price):
            continue
        edge = prob - price - taker_fee(1, price, cfg.fee_rate, cfg.fee_exp)
        if edge >= cfg.margin and (best is None or edge > best[2]):
            best = (side, price, edge, price + edge - cfg.margin)
    return best


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def _find_market(start: int) -> tuple[str, str] | None:
    """(Up token, Down token) for the window starting at `start`."""
    rows = api._get(f"{api.GAMMA}/markets", {"slug": f"btc-updown-5m-{start}"})
    if not rows:
        return None
    m = rows[0]
    tokens, outcomes = json.loads(m["clobTokenIds"]), json.loads(m["outcomes"])
    if outcomes != ["Up", "Down"] or not m.get("acceptingOrders"):
        return None
    return tokens[0], tokens[1]


def run(cfg: BtcConfig, minutes: float | None, log_path: str) -> None:
    weights = load_weights()
    px: dict[int, float] = {}

    def pull(**params) -> None:
        rows = api._get(btc5m.BINANCE, {"symbol": "BTCUSDT", "interval": "1s", **params}, retries=2)
        for r in rows:
            px[r[0] // 1000] = float(r[4])

    print("PAPER TRADING the 5-minute Bitcoin strategy - pretend money, no orders are sent.")
    print(f"${cfg.stake_usd:g} per trade, needs {100 * cfg.margin:g}c of edge, "
          f"decides {cfg.t_min}-{cfg.t_max}s into each window. Press Ctrl+C to stop.\n")
    print("Loading the last 35 minutes of Bitcoin prices...")
    t0 = int(time.time()) - 2100
    for a in range(t0, t0 + 2100, 1000):
        pull(startTime=a * 1000, limit=1000)

    open_trades: list[dict] = []
    closed: list[dict] = []
    total = 0.0
    traded: set[int] = set()
    markets: dict[int, tuple | None] = {}
    last_mark = None
    last_settle_check = 0.0
    noted: set[int] = set()
    deadline = time.time() + minutes * 60 if minutes else None
    new_file = not os.path.exists(log_path)

    def settle() -> None:
        nonlocal total, new_file
        due = [tr for tr in open_trades if tr["start"] + 300 < now]
        if not due:
            return
        meta = btc5m.fetch_meta([tr["start"] for tr in due])
        for tr in due:
            m = meta.get(tr["start"])
            if not m:
                continue          # not settled yet; try again shortly
            won = m["up"] == (tr["side"] == "Up")
            tr["pnl"] = tr["shares"] * (1.0 if won else 0.0) - tr["shares"] * tr["price"] - tr["fee"]
            total += tr["pnl"]
            open_trades.remove(tr)
            closed.append(tr)
            print(f"{_hm(tr['start'])} window settled {'UP' if m['up'] else 'DOWN'}: our {tr['side']} bet "
                  f"{'WON' if won else 'lost'}  ${tr['pnl']:+.2f}   running total ${total:+.2f} "
                  f"({len(closed)} trades)")
            with open(log_path, "a", newline="") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(FIELDS)
                    new_file = False
                w.writerow([datetime.fromtimestamp(tr["start"], timezone.utc).isoformat(), tr["t"], tr["side"],
                            f"{tr['price']:.3f}", f"{tr['est']:.3f}", f"{tr['shares']:.2f}",
                            f"{tr['fee']:.3f}", "won" if won else "lost", f"{tr['pnl']:.2f}", f"{total:.2f}"])

    try:
        while deadline is None or time.time() < deadline or open_trades:
            try:
                pull(limit=5)
            except Exception as e:
                print(f"  (price fetch failed, retrying: {e})")
                time.sleep(2)
                continue
            now = max(px) + 1                 # exchange time, so a wrong PC clock doesn't matter
            for old in [k for k in px if k < now - 2400]:
                del px[old]
            start = now - now % 300
            t = now - start
            mark = (start, t - t % 10)
            accepting = deadline is None or time.time() < deadline

            if (accepting and start not in traded and mark != last_mark
                    and cfg.t_min <= mark[1] <= cfg.t_max):
                last_mark = mark
                try:
                    if start not in markets:
                        markets[start] = _find_market(start)
                    tokens = markets[start]
                    z = btc_move_z(px, start, now)
                    if tokens and z is not None:
                        tops = api.fetch_tops(list(tokens))
                        if all(tk in tops for tk in tokens):
                            ask_up, ask_dn = tops[tokens[0]][2], tops[tokens[1]][2]
                            mid = (ask_up + 1 - ask_dn) / 2
                            p_up = estimate_up(weights, mid, z)
                            if start not in noted:
                                noted.add(start)
                                print(f"{_hm(start)} window: market says Up {mid:.2f}, our estimate {p_up:.2f}")
                            pick = pick_side(p_up, ask_up, ask_dn, cfg)
                            if pick:
                                side, seen, edge, limit = pick
                                time.sleep(1.0)          # our order would arrive about now
                                tops = api.fetch_tops(list(tokens))
                                top = tops.get(tokens[0] if side == "Up" else tokens[1])
                                if top:
                                    price, size = top[2], top[3]
                                    shares = cfg.stake_usd / price
                                    if price <= limit + 1e-9 and size >= shares:
                                        fee = taker_fee(shares, price, cfg.fee_rate, cfg.fee_exp)
                                        est = p_up if side == "Up" else 1 - p_up
                                        open_trades.append({"start": start, "t": t, "side": side, "price": price,
                                                            "est": est, "shares": shares, "fee": fee})
                                        traded.add(start)
                                        print(f"{_hm(start)} window: PAPER BUY {side} {shares:.0f} shares @ {price:.2f} "
                                              f"(we think {est:.2f}), {t}s in")
                                    elif price > limit + 1e-9:
                                        print(f"{_hm(start)} window: {side} looked good at {seen:.2f} but the price "
                                              f"moved to {price:.2f} - skipped")
                                    else:
                                        print(f"{_hm(start)} window: {side} looked good at {price:.2f} but only "
                                              f"{size:.0f} of {shares:.0f} shares were on offer - skipped")
                except Exception as e:
                    print(f"  (market data hiccup, carrying on: {e})")

            if open_trades and time.time() - last_settle_check > 20:
                last_settle_check = time.time()
                try:
                    settle()
                except Exception as e:
                    print(f"  (settlement check failed, will retry: {e})")
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass

    print(f"\nSession result: {len(closed)} settled trade(s), total ${total:+.2f}"
          + (f", {len(open_trades)} still waiting to settle (not counted)" if open_trades else ""))
    if closed:
        wins = sum(1 for tr in closed if tr["pnl"] > 0)
        print(f"Won {wins} of {len(closed)}. Every trade is saved in {log_path}")
