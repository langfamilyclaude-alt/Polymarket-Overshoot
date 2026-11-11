"""Data for Polymarket's 5-minute "Bitcoin Up or Down" markets.

Each market covers one 5-minute window and pays $1 per "Up" share if
Chainlink's Bitcoin price at the end is at or above the price at the start
(the "price to beat"), otherwise $1 per "Down" share.

Everything downloaded here is cached under .cache/btc so it is fetched once.
"""
from __future__ import annotations

import os
import pickle
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import api

BINANCE = "https://data-api.binance.vision/api/v3/klines"
WINDOW = 300
CACHE = os.path.join(".cache", "btc")


def _cached(name: str, build):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    out = build()
    with open(path + ".tmp", "wb") as f:
        pickle.dump(out, f, protocol=4)
    os.replace(path + ".tmp", path)
    return out


def window_starts(end_ts: int, days: float) -> list[int]:
    end = end_ts - end_ts % WINDOW
    return list(range(end - int(days * 86400), end, WINDOW))


def fetch_meta(starts: list[int]) -> dict[int, dict]:
    """start_ts -> {up, beat, final, cond, volume, fee_rate} for finished windows."""
    def batch(chunk):
        params = [("slug", f"btc-updown-5m-{s}") for s in chunk] + [("closed", "true"), ("limit", "100")]
        out = {}
        for m in api._get(f"{api.GAMMA}/markets", params) or []:
            try:
                s = int(m["slug"].rsplit("-", 1)[1])
                prices = m.get("outcomePrices") or ""
                if prices.startswith('["1"'):
                    up = True
                elif prices.startswith('["0"'):
                    up = False
                else:
                    continue    # not cleanly settled
                meta = ((m.get("events") or [{}])[0].get("eventMetadata") or {})
                sched = m.get("feeSchedule") or {}
                out[s] = {"up": up, "beat": meta.get("priceToBeat"), "final": meta.get("finalPrice"),
                          "cond": m["conditionId"], "volume": float(m.get("volumeNum") or 0),
                          "fee_rate": float(sched.get("rate", 0)) if m.get("feesEnabled") else 0.0,
                          "fee_exp": float(sched.get("exponent", 1))}
            except (KeyError, ValueError, IndexError):
                continue
        return out

    chunks = [starts[i:i + 50] for i in range(0, len(starts), 50)]
    meta: dict[int, dict] = {}
    with ThreadPoolExecutor(6) as ex:
        for part in ex.map(batch, chunks):
            meta.update(part)
    return meta


def fetch_klines(interval: str, start_ts: int, end_ts: int) -> np.ndarray:
    """Binance BTCUSDT candles: columns (open time in s, open, high, low, close, volume)."""
    step = {"1s": 1, "1m": 60}[interval] * 1000
    edges = list(range(start_ts, end_ts, step))

    def one(t0):
        rows = api._get(BINANCE, {"symbol": "BTCUSDT", "interval": interval, "limit": 1000,
                                  "startTime": t0 * 1000, "endTime": min(t0 + step, end_ts) * 1000 - 1})
        return [(r[0] // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]

    with ThreadPoolExecutor(6) as ex:
        parts = list(ex.map(one, edges))
    return np.array([r for p in parts for r in p], dtype=np.float64)


def per_second(kl: np.ndarray, start_ts: int, end_ts: int) -> np.ndarray:
    """Close price for every second in [start_ts, end_ts), gaps filled forward."""
    out = np.full(end_ts - start_ts, np.nan)
    idx = kl[:, 0].astype(np.int64) - start_ts
    ok = (idx >= 0) & (idx < len(out))
    out[idx[ok]] = kl[ok, 4]
    last = np.nan
    for i in range(len(out)):          # forward fill
        if np.isnan(out[i]):
            out[i] = last
        else:
            last = out[i]
    return out


TRADE = np.dtype([("t", "i2"), ("kind", "i1"), ("p", "f4"), ("size", "f4")])


def fetch_tape(cond: str, start: int) -> np.ndarray:
    """Trades for one window: t = seconds since window start, p = price of "Up".

    kind +1: someone bought Up (or sold Down) - trade at the Up asking price.
    kind -1: someone sold Up (or bought Down) - trade at the Up bid price.
    """
    rows, _ = api.fetch_trades(cond, start - 120, max_pages=30)
    out = []
    for ts, side, idx, price, size in rows:
        if not (start - 120 <= ts < start + WINDOW + 30):
            continue
        buy_up = (side == "BUY") == (idx == 0)
        out.append((ts - start, 1 if buy_up else -1, price if idx == 0 else 1 - price, size))
    return np.array(out, dtype=TRADE)


def load(days_meta: float = 90, days_tape: float = 10, days_1s: float = 11, now: int | None = None) -> dict:
    """Download (or load from cache) everything the research and backtest need."""
    stamp_path = os.path.join(CACHE, "asof.txt")
    os.makedirs(CACHE, exist_ok=True)
    if now is None and os.path.exists(stamp_path):
        now = int(open(stamp_path).read())
    if now is None:
        now = int(time.time()) - 600        # leave time for the latest windows to settle
        now -= now % WINDOW
        with open(stamp_path, "w") as f:
            f.write(str(now))
    starts = window_starts(now, days_meta)
    meta = _cached(f"meta_{days_meta:g}.pkl", lambda: fetch_meta(starts))
    k1m = _cached(f"k1m_{days_meta:g}.pkl",
                  lambda: fetch_klines("1m", now - int((days_meta + 2) * 86400), now))
    s0 = now - int(days_1s * 86400)
    sec = _cached(f"k1s_{days_1s:g}.pkl", lambda: per_second(fetch_klines("1s", s0, now), s0, now))

    tape_starts = [s for s in window_starts(now, days_tape) if s in meta]

    def tapes():
        def get(s):
            try:
                return s, fetch_tape(meta[s]["cond"], s)
            except Exception:
                return s, np.array([], dtype=TRADE)
        with ThreadPoolExecutor(8) as ex:
            return dict(ex.map(get, tape_starts))

    tape = _cached(f"tape_{days_tape:g}.pkl", tapes)
    return {"now": now, "meta": meta, "k1m": k1m, "sec": sec, "sec_start": s0, "tape": tape}
