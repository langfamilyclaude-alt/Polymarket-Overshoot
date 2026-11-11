"""Strategy and backtest for the 5-minute Bitcoin Up/Down markets.

What the research on 90 days of data showed (see README):
  * Trends over 15 minutes to 24 hours do not predict the next 5 minutes.
  * The move in the last minute before a window does (the market settles on
    a 60-second average price), but the market already prices that in.
  * During a window, the market's price lags the live Bitcoin price a little.

So the strategy is: during each window, estimate the chance of "Up" from two
things - the market's own price and where Bitcoin is right now relative to
the start - and buy whichever side is cheaper than that estimate by more than
the fee plus a safety margin. Positions are held to settlement (no exit fee).

Trend and order-flow measures are computed too, so their (lack of) value can
be tested with `use_trends=True`.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .strategy import taker_fee

TIMES = tuple(range(10, 250, 10))       # decision points, seconds into the window
TRENDS = ("t15", "t60", "t240", "t1440")


@dataclass
class BtcConfig:
    margin: float = 0.05        # required edge per share after fees
    t_min: int = 120            # earliest decision time (s into the window)
    t_max: int = 240            # latest
    min_price: float = 0.05     # don't buy near-certain / near-hopeless sides
    max_price: float = 0.95
    stake_usd: float = 25.0
    fee_rate: float = 0.07
    fee_exp: float = 1.0
    use_trends: bool = False    # add 15m/1h/4h/24h trends to the estimate
    delayed: bool = True        # True: our order arrives >= 1s after the price we saw and
                                # fills only if a later buyer still got our limit price


def build_dataset(data: dict, times=TIMES) -> pd.DataFrame:
    """One row per (window, decision time) with features, quotes and fill prices."""
    meta, sec, s0, tape = data["meta"], data["sec"], data["sec_start"], data["tape"]
    k = data["k1m"]
    kidx = {int(t): i for i, t in enumerate(k[:, 0])}
    close = k[:, 4]

    def minute_close(t):
        i = kidx.get(t - 60)
        return close[i] if i is not None else np.nan

    rows = []
    for s in sorted(tape):
        i0 = s - s0
        tp = tape[s]
        if i0 < 1900 or len(tp) < 20:
            continue
        base = sec[i0 - 60:i0].mean()                      # stand-in for the 60s-average start price
        sig = np.diff(np.log(sec[i0 - 1800:i0])).std() * 1e4   # recent volatility, bps per sqrt(second)
        if not sig > 0:
            continue
        c0 = sec[i0 - 1]
        trend = {n: (c0 / minute_close(s - h) - 1) * 1e4 / (sig * np.sqrt(h))
                 for n, h in zip(TRENDS, (900, 3600, 14400, 86400))}
        tt, kd, pp, sz = tp["t"], tp["kind"], tp["p"], tp["size"]
        for t in times:
            # Bitcoin's move so far, in units of "typical move over the time left".
            # Uses the price one second ago, i.e. data we would really have.
            move = (sec[i0 + t - 1] / base - 1) * 1e4
            z = move / (sig * np.sqrt(260 - t))
            recent = (tt > t - 15) & (tt <= t)
            asks, bids = pp[recent & (kd == 1)], pp[recent & (kd == -1)]
            nxt = (tt > t) & (tt <= t + 5)
            fa, fb = pp[nxt & (kd == 1)], pp[nxt & (kd == -1)]
            # Later fills: the first same-side trade at least one second AFTER
            # the trade we saw, within 5 more seconds (None if there was none).
            def later(mask_kind, first_idx):
                if first_idx is None:
                    return np.nan
                t1 = tt[first_idx]
                m = (tt > t1) & (tt <= t1 + 5) & mask_kind
                return pp[m][0] if m.any() else np.nan
            ia = np.flatnonzero(nxt & (kd == 1))
            ib = np.flatnonzero(nxt & (kd == -1))
            la = later(kd == 1, ia[0] if len(ia) else None)
            lb = later(kd == -1, ib[0] if len(ib) else None)
            fl = (tt > t - 30) & (tt <= t)
            vol = float(sz[fl].sum())
            rows.append({
                "s": s, "t": t, "up": bool(meta[s]["up"]), "z": z,
                "ask": asks[-1] if len(asks) else np.nan,
                "bid": bids[-1] if len(bids) else np.nan,
                "buy_up": fa[0] if len(fa) else np.nan,          # what the next Up buyer paid
                "buy_dn": 1 - fb[0] if len(fb) else np.nan,      # what the next Down buyer paid
                "buy_up_late": la, "buy_dn_late": 1 - lb,        # ...and the buyer after that
                "flow": float((kd[fl] * sz[fl]).sum()) / vol if vol > 0 else 0.0,
                **trend,
            })
    df = pd.DataFrame(rows)
    # The market's current view of "Up": halfway between what it costs to buy
    # Up and what you'd effectively get for it (1 - cost of Down), taken from
    # the same trades we would fill against, so it is never out of date.
    df["mid"] = (df.buy_up + (1 - df.buy_dn)) / 2
    df["stale_mid"] = (df.ask + df.bid) / 2
    return df


def _logit(p):
    p = np.clip(p, 0.01, 0.99)
    return np.log(p / (1 - p))


def _features(df: pd.DataFrame, cfg: BtcConfig) -> np.ndarray:
    cols = [np.ones(len(df)), _logit(df.mid.values), df.z.values]
    if cfg.use_trends:
        cols += [df[c].values for c in TRENDS] + [df.flow.values]
    return np.column_stack(cols)


def fit(df: pd.DataFrame, cfg: BtcConfig) -> np.ndarray:
    """Logistic regression (chance of Up) on rows inside the trading time range."""
    d = df[(df.t >= cfg.t_min) & (df.t <= cfg.t_max)].dropna(subset=["mid", "z", *TRENDS])
    X, y = _features(d, cfg), d.up.values.astype(float)
    w = np.zeros(X.shape[1])
    for _ in range(50):
        p = 1 / (1 + np.exp(-X @ w))
        H = X.T @ (X * (p * (1 - p) + 1e-9)[:, None]) + 1e-3 * np.eye(len(w))
        w += np.linalg.solve(H, X.T @ (y - p))
    return w


def simulate(df: pd.DataFrame, w: np.ndarray, cfg: BtcConfig) -> pd.DataFrame:
    """At most one trade per window: the first time an edge clears the margin."""
    d = df[(df.t >= cfg.t_min) & (df.t <= cfg.t_max)].dropna(subset=["mid", "z", *TRENDS]).copy()
    d["p"] = 1 / (1 + np.exp(-_features(d, cfg) @ w))
    trades = []
    for s, g in d.groupby("s", sort=True):
        for r in g.sort_values("t").itertuples():
            best = None
            for side, price, late, prob in (("Up", r.buy_up, r.buy_up_late, r.p),
                                            ("Down", r.buy_dn, r.buy_dn_late, 1 - r.p)):
                if not (cfg.min_price <= price <= cfg.max_price):   # also false for NaN
                    continue
                fee = taker_fee(1, price, cfg.fee_rate, cfg.fee_exp)
                edge = prob - price - fee
                if edge >= cfg.margin and (best is None or edge > best[0]):
                    best = (edge, side, price, late, prob)
            if best:
                edge, side, price, late, prob = best
                if cfg.delayed:
                    # Limit order at the highest price that still leaves the margin.
                    # (Fee changes slightly with price; the seen price's fee is close enough.)
                    limit = price + (edge - cfg.margin)
                    if not late <= limit + 1e-9:      # no later fill at our price (or NaN)
                        continue
                    price = late
                    edge = prob - price - taker_fee(1, price, cfg.fee_rate, cfg.fee_exp)
                fee = taker_fee(1, price, cfg.fee_rate, cfg.fee_exp)
                shares = cfg.stake_usd / price
                won = r.up == (side == "Up")
                trades.append({"s": s, "t": r.t, "side": side, "price": price, "est": prob,
                               "edge": edge, "won": won, "fees": fee * shares,
                               "pnl": shares * (1.0 if won else 0.0) - cfg.stake_usd - fee * shares})
                break
    return pd.DataFrame(trades)


def walk_forward(df: pd.DataFrame, cfg: BtcConfig, train_days: int = 6) -> pd.DataFrame:
    """Each day is traded with a model fitted only on the `train_days` before it."""
    day = df.s // 86400
    days = sorted(day.unique())
    parts = []
    for i in range(train_days, len(days)):
        train = df[(day >= days[i - train_days]) & (day < days[i])]
        parts.append(simulate(df[day == days[i]], fit(train, cfg), cfg))
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def baseline(df: pd.DataFrame, feature: str, cfg: BtcConfig, t: int = 10, threshold: float = 0.0) -> pd.DataFrame:
    """Naive rule: early in every window, buy the side `feature` points to."""
    d = df[df.t == t].dropna(subset=[feature])
    trades = []
    for r in d.itertuples():
        v = getattr(r, feature)
        if abs(v) <= threshold:
            continue
        side, price = ("Up", r.buy_up) if v > 0 else ("Down", r.buy_dn)
        if not (cfg.min_price <= price <= cfg.max_price):
            continue
        shares = cfg.stake_usd / price
        fee = taker_fee(shares, price, cfg.fee_rate, cfg.fee_exp)
        won = r.up == (side == "Up")
        trades.append({"s": r.s, "t": t, "side": side, "price": price, "won": won, "fees": fee,
                       "pnl": shares * (1.0 if won else 0.0) - cfg.stake_usd - fee})
    return pd.DataFrame(trades)


def summary(tr: pd.DataFrame, stake: float = 25.0) -> dict:
    if not len(tr):
        return {"trades": 0, "pnl": 0.0, "win_rate": 0.0, "avg_price": 0.0, "per_trade": 0.0,
                "t_stat": 0.0, "fees": 0.0, "roi": 0.0, "worst_day": 0.0, "days_up": 0, "days": 0}
    daily = tr.groupby(tr.s // 86400).pnl.sum()
    se = tr.pnl.std(ddof=1) / np.sqrt(len(tr)) if len(tr) > 1 else float("nan")
    return {
        "trades": len(tr), "pnl": float(tr.pnl.sum()), "win_rate": float(tr.won.mean()),
        "avg_price": float(tr.price.mean()), "per_trade": float(tr.pnl.mean()),
        "t_stat": float(tr.pnl.mean() / se) if se and se > 0 else 0.0,
        "fees": float(tr.fees.sum()), "roi": float(tr.pnl.sum() / (stake * len(tr))),
        "worst_day": float(daily.min()), "days_up": int((daily > 0).sum()), "days": len(daily),
    }
