"""Command line: scan, backtest, paper.

    python -m overshoot scan        # what looks stretched or inconsistent right now
    python -m overshoot backtest    # replay recent history through the strategy
    python -m overshoot paper       # trade live prices with pretend money
    python -m overshoot mm-backtest # replay real trades through a market maker
    python -m overshoot arb-scan    # events whose prices don't add up to $1
    python -m overshoot arb-watch   # ...watched over time
    python -m overshoot arb-backtest
    python -m overshoot btc-backtest # 5-minute Bitcoin Up/Down markets
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import time
from datetime import datetime, timedelta, timezone

from . import api
from .config import Config
from .strategy import Detector, Engine, summarize, taker_fee


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M:%S")


def _print_summary(s: dict) -> None:
    if not s["trades"]:
        print("No trades were triggered.")
        return
    print(f"  trades            {s['trades']}")
    print(f"  win rate          {s['win_rate']:.0%}")
    print(f"  net P&L           ${s['net_pnl']:+.2f}")
    print(f"  P&L before fees   ${s['pnl_before_fees']:+.2f}   (fees paid ${s['fees_paid']:.2f})")
    print(f"  avg per trade     ${s['avg_pnl_per_trade']:+.3f}   (avg win ${s['avg_win']:+.2f}, avg loss ${s['avg_loss']:+.2f})")
    print(f"  return on staked  {s['return_on_staked']:+.2%}")
    for reason, (n, pnl) in s["by_exit"].items():
        print(f"  exit={reason:7} {n:4} trades  ${pnl:+.2f}")


# --------------------------------------------------------------------- scan
def cmd_scan(cfg: Config, args) -> None:
    markets = [m for m in api.fetch_markets(cfg) if api.tradable(m, cfg)]
    if args.live_only:
        markets = [m for m in markets if m.is_live()]
    markets = markets[:cfg.max_markets]
    now = int(time.time())
    rows = []
    for m in markets:
        hist = api.fetch_history(m.token_ids[0], now - cfg.baseline_s - cfg.lookback_s - 600, now)
        det, last = Detector(cfg), None
        z = spike = 0.0
        for ts, p in hist:
            sig = det.update(ts, p)
            last = sig
        if hist:  # plain price change over the lookback window
            then = [p for ts, p in hist if ts <= hist[-1][0] - cfg.lookback_s]
            spike = hist[-1][1] - (then[-1] if then else hist[0][1])
        if last:
            z = last.z
        rows.append((z, spike, m))
    rows.sort(key=lambda r: (-r[0], -abs(r[1])))
    print(f"{len(rows)} markets watched ({'live games only' if args.live_only else 'all types'}). "
          f"Markets with an active overshoot signal are listed first.\n")
    print(f"{'signal':>7} {'5m move':>7} {'bid':>6} {'ask':>6} {'vol24h':>10} live  market")
    for z, spike, m in rows:
        flag = f"z={z:4.1f}" if z else "      -"
        print(f"{flag:>7} {spike:+7.3f} {m.best_bid:6.3f} {m.best_ask:6.3f} "
              f"{m.volume_24h:10,.0f} {'yes' if m.is_live() else ' no'}  {m.question[:70]}")
    print("\nFor price-sum inconsistencies in multi-outcome events, run: python -m overshoot arb-scan")


# ----------------------------------------------------------------- backtest
def cmd_backtest(cfg: Config, args) -> None:
    now = int(time.time())
    start = now - int(args.days * 86400)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    # Selected on activity only - NOT on today's price - and finished markets
    # are included, so games that ended badly for the strategy still count.
    pool = api.fetch_markets(cfg, pages=3) + api.fetch_markets(cfg, pages=3, closed_since=since)
    seen, markets = set(), []
    for m in sorted(pool, key=lambda m: -m.volume_24h):
        if m.condition_id in seen or m.volume_24h < cfg.min_volume_24h:
            continue
        if args.live_only and m.game_start is None:
            continue
        seen.add(m.condition_id)
        markets.append(m)
    markets = markets[:args.markets]

    ticks = []  # (ts, market index, price of outcome 0)
    for i, m in enumerate(markets):
        hist = api.fetch_history(m.token_ids[0], start, now)
        if args.live_only and m.game_start:
            # keep the pre-game baseline, but nothing more than needed
            g = m.game_start.timestamp() - cfg.baseline_s - cfg.lookback_s
            hist = [(t, p) for t, p in hist if t >= g]
        ticks += [(t, i, p) for t, p in hist]
    ticks.sort()
    print(f"Replaying {len(ticks):,} one-minute prices from {len(markets)} markets "
          f"over the last {args.days:g} day(s)...")

    eng = Engine(cfg)
    half = cfg.backtest_spread / 2
    last: dict[str, tuple[float, float]] = {}
    for ts, i, p in ticks:
        m = markets[i]
        if args.live_only and m.game_start and ts < m.game_start.timestamp():
            eng.detectors.setdefault(m.condition_id, Detector(cfg)).update(ts, p)
            continue
        bid0, ask0 = max(p - half, 0.001), min(p + half, 0.999)
        bid1, ask1 = 1 - ask0, 1 - bid0
        last[m.condition_id] = (bid0, bid1)
        for line in eng.on_quote(m.condition_id, m.question, ts, bid0, ask0, bid1, ask1,
                                 m.tick, m.fee_rate, m.fee_exponent,
                                 m.min_size, m.event_id):
            if args.verbose:
                print(_hhmm(ts), line)
    eng.close_all(ticks[-1][0] if ticks else now, last)
    print("\nBacktest result:")
    s = summarize(eng.trades)
    _print_summary(s)
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"config": dataclasses.asdict(cfg), "summary": s,
                       "trades": [dataclasses.asdict(t) for t in eng.trades]}, f, indent=1)
        print(f"\nDetails written to {args.json}")


# -------------------------------------------------------------------- paper
def cmd_paper(cfg: Config, args) -> None:
    eng = Engine(cfg)
    markets: dict[str, api.Market] = {}
    next_refresh = 0.0
    logged = 0
    deadline = time.time() + args.minutes * 60 if args.minutes else None
    new_file = not os.path.exists(args.log)
    print("PAPER TRADING - pretend money only, no orders are sent. Ctrl+C to stop.")
    try:
        while deadline is None or time.time() < deadline:
            now = time.time()
            if now >= next_refresh:
                fresh = [m for m in api.fetch_markets(cfg) if api.tradable(m, cfg)]
                if args.live_only:
                    fresh = [m for m in fresh if m.is_live()]
                fresh = {m.condition_id: m for m in fresh[:cfg.max_markets]}
                for key in eng.positions:          # never drop a market we hold
                    fresh.setdefault(key, markets[key])
                for key, m in fresh.items():       # warm up new markets from history
                    if key not in eng.detectors:
                        det = eng.detectors[key] = Detector(cfg)
                        for ts, p in api.fetch_history(
                                m.token_ids[0], int(now) - cfg.baseline_s - cfg.lookback_s, int(now) - 30):
                            det.update(ts, p)
                markets = fresh
                next_refresh = now + 600
                print(f"{_hhmm(now)} watching {len(markets)} markets")
            try:
                tops = api.fetch_tops([t for m in markets.values() for t in m.token_ids])
            except Exception as e:  # network blips shouldn't kill the loop
                print(f"{_hhmm(now)} quote fetch failed: {e}")
                time.sleep(cfg.poll_s)
                continue
            for key, m in markets.items():
                a, b = tops.get(m.token_ids[0]), tops.get(m.token_ids[1])
                if not a or not b:
                    continue
                for line in eng.on_quote(key, m.question, now, a[0], a[2], b[0], b[2],
                                         m.tick, m.fee_rate, m.fee_exponent, m.min_size, m.event_id):
                    print(_hhmm(now), line)
            if len(eng.trades) > logged:
                with open(args.log, "a", newline="") as f:
                    w = csv.writer(f)
                    if new_file:
                        w.writerow([fl.name for fl in dataclasses.fields(eng.trades[0])])
                        new_file = False
                    for t in eng.trades[logged:]:
                        w.writerow(dataclasses.astuple(t))
                logged = len(eng.trades)
            time.sleep(cfg.poll_s)
    except KeyboardInterrupt:
        pass
    print(f"\nPaper session result ({len(eng.positions)} position(s) still open, not counted):")
    _print_summary(summarize(eng.trades))


# -------------------------------------------------------------- mm-backtest
def load_tapes(cfg: Config, days: float, n_markets: int, cache_dir: str):
    """Markets (open + recently finished) and every trade in them, cached on disk."""
    import pickle
    from concurrent.futures import ThreadPoolExecutor

    os.makedirs(cache_dir, exist_ok=True)
    index = os.path.join(cache_dir, f"tapes_{days:g}d_{n_markets}.pkl")
    if os.path.exists(index):
        with open(index, "rb") as f:
            return pickle.load(f)
    now = int(time.time())
    start = now - int(days * 86400)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    pool = api.fetch_markets(cfg, pages=3) + api.fetch_markets(cfg, pages=4, closed_since=since)
    seen, markets = set(), []
    for m in sorted(pool, key=lambda m: -m.volume_24h):
        if m.condition_id not in seen and m.volume_24h >= cfg.min_volume_24h:
            seen.add(m.condition_id)
            markets.append(m)
    markets = markets[:n_markets]
    print(f"Downloading every trade from {len(markets)} markets (cached afterwards)...")

    def get(m):
        try:
            return api.fetch_trades(m.condition_id, start)
        except Exception as e:
            print(f"  skipped {m.question[:50]}: {e}")
            return [], False

    with ThreadPoolExecutor(6) as ex:
        tapes = list(ex.map(get, markets))
    out = (markets, tapes, now)
    with open(index, "wb") as f:
        pickle.dump(out, f)
    return out


def _mm_job(job):
    from .maker import MakerConfig, combine, normalize, simulate
    label, mc, markets, tapes = job
    results = []
    for m, (rows, _) in zip(markets, tapes):
        if not rows:
            continue
        if mc.phase == "other" and m.game_start is not None:
            continue
        if mc.phase in ("pregame", "ingame") and m.game_start is None:
            continue
        results.append(simulate(
            normalize(rows, m.tick), mc, tick=m.tick,
            game_start=m.game_start.timestamp() if m.game_start else None,
            fee_rate=m.fee_rate, fee_exp=m.fee_exponent, rebate_rate=m.rebate_rate,
            label=m.question))
    return combine(results, label)


def cmd_mm_backtest(cfg: Config, args) -> None:
    from multiprocessing import Pool

    from .maker import MakerConfig
    markets, tapes, _ = load_tapes(cfg, args.days, args.markets, args.cache_dir)
    n_trades = sum(len(t[0]) for t in tapes)
    cut = sum(1 for t in tapes if not t[1])
    print(f"{len(markets)} markets, {n_trades:,} real trades over {args.days:g} day(s)."
          + (f" ({cut} very busy markets were cut short.)" if cut else ""))
    base = MakerConfig()
    for item in args.mm:
        name, _, value = item.partition("=")
        if not hasattr(base, name):
            raise SystemExit(f"unknown market-maker setting: {name}")
        kind = type(getattr(base, name))
        setattr(base, name, value.lower() in ("1", "true", "yes") if kind is bool else kind(value))
    queues = [("no queue (best case)", dict(queue_usd=0.0)),
              ("$1k ahead of us", dict(queue_usd=1000.0)),
              ("$10k ahead of us", dict(queue_usd=10000.0)),
              ("worst case", dict(through_only=True))]
    phases = [("before games", "pregame"), ("during games", "ingame"), ("non-sports", "other")]
    jobs = [(f"{pn:13} {qn}", dataclasses.replace(base, phase=ph, **q), markets, tapes)
            for pn, ph in phases for qn, q in queues]
    with Pool(min(4, os.cpu_count() or 1)) as pool:
        rows = pool.map(_mm_job, jobs)
    print(f"\n{'when':13} {'queue assumption':21} {'mkts':>4} {'fills':>7} {'traded $':>10} "
          f"{'P&L $':>9} {'per $100':>8} {'60s move':>8} {'rebates?':>8}")
    for r in rows:
        print(f"{r['label']:35} {r['markets_traded']:4} {r['fills']:7,} {r['volume_usd']:10,.0f} "
              f"{r['pnl']:+9.2f} {r['pnl_per_100_traded']:+8.2f} {r['markout_60s_cents']:+7.2f}c "
              f"{r['rebate_est']:8.2f}")
    print("\n'60s move' = how far the price moved in our favour (+) or against us (-) in the"
          "\nminute after each fill, in cents per share. 'rebates?' is a possible maker rebate,"
          "\nNOT included in P&L and not verified.")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"maker_config": dataclasses.asdict(base), "rows": rows}, f, indent=1)


# ---------------------------------------------------------------------- arb
def cmd_arb_scan(cfg: Config, args) -> None:
    from . import arb
    rows, opps = arb.scan(cfg, args.events)
    print(f"{len(rows)} 'exactly one outcome wins' events checked against live order books.")
    print("Cost of a basket that pays $1.00, fees included (below 1.000 = locked-in profit):\n")
    print(f"{'buy all YES':>11} {'buy all NO':>10} {'outcomes':>8}  event")
    for g, cy, cn in sorted(rows, key=lambda r: min(r[1] or 9, r[2] or 9))[:args.top]:
        f = lambda v: f"{v:.3f}" if v is not None else "  n/a"
        print(f"{f(cy):>11} {f(cn):>10} {len(g.markets):8}  {g.title[:70]}")
    print()
    if not opps:
        print("No profitable basket right now.")
    for o in opps:
        print(f"PROFITABLE: buy all {o.kind} in '{o.title[:60]}': {o.sets:,.0f} baskets available, "
              f"total profit ${o.profit:.2f} (paid out when the event resolves)")


def cmd_arb_watch(cfg: Config, args) -> None:
    from . import arb
    end = time.time() + args.minutes * 60
    live: dict[tuple, dict] = {}
    done: list[dict] = []
    polls = 0
    print(f"Watching for {args.minutes:g} minutes, checking every {args.every:g}s. Nothing is traded.")
    while time.time() < end:
        now = time.time()
        try:
            _, opps = arb.scan(cfg, args.events)
        except Exception as e:
            print(f"{_hhmm(now)} check failed: {e}")
            time.sleep(args.every)
            continue
        polls += 1
        seen = set()
        for o in opps:
            key = (o.title, o.kind)
            seen.add(key)
            ep = live.get(key)
            if ep is None:
                ep = live[key] = {"title": o.title, "kind": o.kind, "start": now, "profit": o.profit}
                print(f"{_hhmm(now)} APPEARED  all-{o.kind} '{o.title[:50]}' profit ${o.profit:.2f} on {o.sets:,.0f} baskets")
            ep["profit"] = max(ep["profit"], o.profit)
            ep["end"] = now
        for key in [k for k in live if k not in seen]:
            ep = live.pop(key)
            done.append(ep)
            print(f"{_hhmm(now)} GONE      all-{ep['kind']} '{ep['title'][:50]}' lasted {ep['end'] - ep['start']:.0f}s")
        time.sleep(args.every)
    done += live.values()
    print(f"\n{polls} checks. {len(done)} opportunity(ies) seen; "
          f"largest profit available at any one moment: ${max((e['profit'] for e in done), default=0):.2f}; "
          f"sum of each one's best moment: ${sum(e['profit'] for e in done):.2f}")
    for e in done:
        print(f"  ${e['profit']:.2f}  lasted {e.get('end', e['start']) - e['start']:.0f}s  all-{e['kind']}  {e['title'][:60]}")


def cmd_arb_backtest(cfg: Config, args) -> None:
    import pickle
    from concurrent.futures import ThreadPoolExecutor

    from . import arb
    os.makedirs(args.cache_dir, exist_ok=True)
    path = os.path.join(args.cache_dir, f"arb_{args.days:g}d_{args.events}.pkl")
    if os.path.exists(path):
        with open(path, "rb") as f:
            groups, tapes = pickle.load(f)
    else:
        # Only events where every outcome is still open: the basket was then
        # the same throughout the period being replayed.
        groups = [g for g in arb.load_groups(cfg, max_outcomes=args.max_outcomes) if g.all_open][:args.events]
        start = int(time.time() - args.days * 86400)
        print(f"Downloading trades for {sum(len(g.markets) for g in groups)} outcomes in {len(groups)} events...")
        with ThreadPoolExecutor(6) as ex:
            tapes = [list(ex.map(lambda m: api.fetch_trades(m.condition_id, start)[0], g.markets)) for g in groups]
        with open(path, "wb") as f:
            pickle.dump((groups, tapes), f)
    n_trades = sum(len(t) for ts in tapes for t in ts)
    print(f"{len(groups)} events, {n_trades:,} real trades over {args.days:g} day(s).\n")
    print(f"{'every leg traded within':>23} {'events priced':>13} {'times priced':>12} "
          f"{'cheapest basket':>15} {'profitable moments':>18} {'total profit':>12}")
    detail = []
    for fresh in (30, 60, 300, 900):
        priced = n_priced = 0
        best = None
        eps = []
        for g, ts in zip(groups, tapes):
            r = arb.backtest(g, ts, fresh)
            k = r["priced_yes"] + r["priced_no"]
            n_priced += k
            priced += k > 0
            for b in (r["best_yes"], r["best_no"]):
                if b is not None:
                    best = b if best is None else min(best, b)
            eps += [dict(e, title=g.title) for e in r["episodes"]]
        total = sum(e["edge"] * e["sets"] for e in eps)
        print(f"{str(fresh) + ' seconds':>23} {priced:13} {n_priced:12,} "
              f"{(f'{best:.3f}' if best is not None else 'n/a'):>15} {len(eps):18} {('$%.2f' % total):>12}")
        if fresh == args.detail:
            detail = eps
    if detail:
        print(f"\nLargest at {args.detail}s freshness:")
        for e in sorted(detail, key=lambda e: -e["edge"] * e["sets"])[:8]:
            print(f"  ${e['edge'] * e['sets']:7.2f}  edge {100 * e['edge']:.1f}c x {e['sets']:,.0f} baskets, "
                  f"all-{e['kind']}, lasted {e['end'] - e['start']:.0f}s  {e['title'][:50]}")


# ------------------------------------------------------------- btc-backtest
def cmd_btc_backtest(cfg: Config, args) -> None:
    import pandas as pd

    from . import btc5m
    from . import btc_strategy as bs
    data = btc5m.load()
    path = os.path.join(btc5m.CACHE, "dataset.pkl")
    if os.path.exists(path):
        df = pd.read_pickle(path)
    else:
        print("Preparing the dataset (a minute or two, cached afterwards)...")
        df = bs.build_dataset(data)
        df.to_pickle(path)
    split = data["now"] - int(args.test_days * 86400)
    design, test = df[df.s < split], df[df.s >= split]
    base = bs.BtcConfig()
    for item in args.btc:
        name, _, value = item.partition("=")
        if not hasattr(base, name):
            raise SystemExit(f"unknown setting: {name}")
        kind = type(getattr(base, name))
        setattr(base, name, value.lower() in ("1", "true", "yes") if kind is bool else kind(value))
    print(f"Settings were chosen using the {design.s.nunique():,} windows BEFORE the test period.")
    print(f"Test period: the last {args.test_days:g} days, {test.s.nunique():,} five-minute windows, "
          f"${base.stake_usd:g} per trade.\n")
    rows = []
    variants = [
        ("Strategy, realistic (1s+ delay)", dataclasses.replace(base, delayed=True)),
        ("Strategy, if we were the fastest", dataclasses.replace(base, delayed=False)),
        ("Strategy + trends, realistic", dataclasses.replace(base, delayed=True, use_trends=True)),
    ]
    for name, c in variants:
        w = bs.fit(design, c)
        rows.append((name, bs.summary(bs.simulate(test, w, c), c.stake_usd)))
    for name, feat in (("Simple: follow last-minute move", "z"), ("Simple: follow 15-minute trend", "t15"),
                       ("Simple: follow 1-hour trend", "t60"), ("Simple: follow 4-hour trend", "t240"),
                       ("Simple: follow 24-hour trend", "t1440")):
        rows.append((name, bs.summary(bs.baseline(test, feat, base), base.stake_usd)))
    print(f"{'':34} {'trades':>6} {'win %':>6} {'avg paid':>8} {'P&L $':>9} {'per trade':>9} "
          f"{'fees $':>8} {'days up':>7} {'signif.':>7}")
    for name, s in rows:
        print(f"{name:34} {s['trades']:6} {100 * s['win_rate']:5.1f}% {s['avg_price']:8.3f} "
              f"{s['pnl']:+9.2f} {s['per_trade']:+9.2f} {s['fees']:8.2f} "
              f"{s['days_up']:>3}/{s['days']:<3} {s['t_stat']:+7.1f}")
    print("\n'signif.' = average profit divided by its margin of error. Below about 2 (either"
          "\nsign) the result cannot be told apart from luck.")
    if args.history_days:
        _btc_walk_forward(args.history_days, base, split, design.s.min())
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"config": dataclasses.asdict(base), "rows": rows}, f, indent=1)


def _btc_walk_forward(days: float, base, test_start: int, design_start: int) -> None:
    """Same locked settings over a longer stretch; each day uses a model fitted
    on the six days before it."""
    import pandas as pd

    from . import btc5m
    from . import btc_strategy as bs
    data = btc5m.load(days_tape=days, days_1s=days + 1)
    path = os.path.join(btc5m.CACHE, f"dataset_{days:g}.pkl")
    if os.path.exists(path):
        df = pd.read_pickle(path)
    else:
        print("\nPreparing the longer dataset (several minutes, cached afterwards)...")
        df = bs.build_dataset(data)
        df.to_pickle(path)
    print(f"\nLonger check: {df.s.nunique():,} windows over {days:g} days, same settings, "
          f"model refitted daily on the previous 6 days.")
    print(f"{'':34} {'period':24} {'trades':>6} {'win %':>6} {'avg paid':>8} {'P&L $':>9} "
          f"{'per trade':>9} {'days up':>7} {'signif.':>7}")
    periods = [("earlier (never used to tune)", lambda t: t.s < design_start),
               ("tuning days", lambda t: (t.s >= design_start) & (t.s < test_start)),
               ("last few days", lambda t: t.s >= test_start),
               ("ALL", lambda t: t.s > 0)]
    for name, c in (("Strategy, realistic (1s+ delay)", dataclasses.replace(base, delayed=True)),
                    ("Strategy, if we were the fastest", dataclasses.replace(base, delayed=False))):
        tr = bs.walk_forward(df, c)
        for pname, sel in periods:
            s = bs.summary(tr[sel(tr)] if len(tr) else tr, c.stake_usd)
            print(f"{name:34} {pname:24} {s['trades']:6} {100 * s['win_rate']:5.1f}% {s['avg_price']:8.3f} "
                  f"{s['pnl']:+9.2f} {s['per_trade']:+9.2f} {s['days_up']:>3}/{s['days']:<3} {s['t_stat']:+7.1f}")
    for name, feat in (("Simple: follow last-minute move", "z"), ("Simple: follow 1-hour trend", "t60"),
                       ("Simple: follow 24-hour trend", "t1440")):
        s = bs.summary(bs.baseline(df, feat, base), base.stake_usd)
        print(f"{name:34} {'ALL':24} {s['trades']:6} {100 * s['win_rate']:5.1f}% {s['avg_price']:8.3f} "
              f"{s['pnl']:+9.2f} {s['per_trade']:+9.2f} {s['days_up']:>3}/{s['days']:<3} {s['t_stat']:+7.1f}")


def main() -> None:
    cfg = Config()
    ap = argparse.ArgumentParser(prog="overshoot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="override a Config setting, e.g. --set z_enter=5 (repeatable)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="show stretched / inconsistent markets now")
    p.add_argument("--live-only", action="store_true", help="only games in progress")
    p = sub.add_parser("backtest", help="replay recent history")
    p.add_argument("--days", type=float, default=3)
    p.add_argument("--markets", type=int, default=60)
    p.add_argument("--live-only", action="store_true", help="only sports/esports, only once the game has started")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--json", help="write trades and summary to this file")
    p = sub.add_parser("paper", help="trade live prices with pretend money")
    p.add_argument("--live-only", action="store_true", help="only games in progress")
    p.add_argument("--minutes", type=float, help="stop after this long (default: run until Ctrl+C)")
    p.add_argument("--log", default="paper_trades.csv")
    p = sub.add_parser("mm-backtest", help="replay real trades through the market-making strategy")
    p.add_argument("--days", type=float, default=3)
    p.add_argument("--markets", type=int, default=150)
    p.add_argument("--mm", action="append", default=[], metavar="NAME=VALUE",
                   help="override a MakerConfig setting, e.g. --mm improve=true (repeatable)")
    p.add_argument("--cache-dir", default=".cache")
    p.add_argument("--json", help="write the result table to this file")
    p = sub.add_parser("arb-scan", help="multi-outcome events whose prices don't add up, right now")
    p.add_argument("--events", type=int, default=80)
    p.add_argument("--top", type=int, default=12)
    p = sub.add_parser("arb-watch", help="repeat arb-scan for a while and time what appears")
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("--every", type=float, default=10)
    p.add_argument("--events", type=int, default=80)
    p = sub.add_parser("arb-backtest", help="look for past price-sum gaps in real trades")
    p.add_argument("--days", type=float, default=3)
    p.add_argument("--events", type=int, default=60)
    p.add_argument("--max-outcomes", type=int, default=12)
    p.add_argument("--detail", type=int, default=60, choices=[30, 60, 300, 900])
    p.add_argument("--cache-dir", default=".cache")
    p = sub.add_parser("btc-backtest", help="5-minute Bitcoin Up/Down strategy on the last few days")
    p.add_argument("--test-days", type=float, default=4)
    p.add_argument("--history-days", type=float, default=0,
                   help="also run the same settings over this many days (big download the first time)")
    p.add_argument("--btc", action="append", default=[], metavar="NAME=VALUE",
                   help="override a BtcConfig setting, e.g. --btc margin=0.08 (repeatable)")
    p.add_argument("--json")
    args = ap.parse_args()
    for item in args.set:
        name, _, value = item.partition("=")
        if not hasattr(cfg, name):
            ap.error(f"unknown setting: {name}")
        setattr(cfg, name, type(getattr(cfg, name))(value))
    {"scan": cmd_scan, "backtest": cmd_backtest, "paper": cmd_paper,
     "mm-backtest": cmd_mm_backtest, "arb-scan": cmd_arb_scan,
     "arb-watch": cmd_arb_watch, "arb-backtest": cmd_arb_backtest,
     "btc-backtest": cmd_btc_backtest}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
