"""Price-sum check for "exactly one outcome wins" events.

In such an event, holding one YES share of every outcome pays exactly $1, and
holding one NO share of every outcome pays (number of outcomes - 1) dollars.
If either basket can be bought for less than it pays, after fees, the
difference is locked-in profit regardless of who wins.

Three ways to look for it:
  check()    - against real order books right now (the reliable one)
  watch      - check() repeatedly for a while, to see how often/long it appears
  backtest() - against past trades; prices are inferred, so treat as a rough guide
"""
from __future__ import annotations

from dataclasses import dataclass

from . import api
from .strategy import taker_fee


@dataclass
class Group:
    title: str
    markets: list[api.Market]   # the still-open outcomes
    volume_24h: float
    all_open: bool = True       # False if some outcomes have already been ruled out


@dataclass
class Opportunity:
    title: str
    kind: str        # "YES" or "NO" basket
    cost: float      # per basket, including fees
    payout: float
    sets: float      # baskets available at the best prices

    @property
    def profit(self) -> float:
        return (self.payout - self.cost) * self.sets


def load_groups(cfg, pages: int = 3, max_outcomes: int = 30) -> list[Group]:
    """Events where the open outcomes are guaranteed to contain the winner."""
    groups = []
    for ev in api.fetch_events(pages):
        # "Augmented" events can gain new outcomes later, so their prices
        # need not add up to $1.
        if not ev.get("negRisk") or ev.get("negRiskAugmented"):
            continue
        open_ms, ok, all_open = [], True, True
        for raw in ev.get("markets") or []:
            if raw.get("closed"):
                all_open = False
                # A closed outcome has lost - unless it WON, in which case
                # the event is over and the rest are worthless.
                if str(raw.get("outcomePrices", "")).startswith('["1"'):
                    ok = False
                continue
            m = api.parse_market(raw, cfg.default_fee_rate, cfg.default_fee_exponent)
            if m is None:   # an outcome we can't buy -> can't complete the basket
                ok = False
                break
            open_ms.append(m)
        if ok and 2 <= len(open_ms) <= max_outcomes:
            groups.append(Group(ev.get("title", ""), open_ms, float(ev.get("volume24hr") or 0), all_open))
    groups.sort(key=lambda g: -g.volume_24h)
    return groups


def basket_cost(prices: list[float], markets: list[api.Market]) -> float:
    """Cost of buying one share at each price as a taker, fees included."""
    return sum(p + taker_fee(1, p, m.fee_rate, m.fee_exponent) for p, m in zip(prices, markets))


def check(group: Group, tops: dict) -> tuple[float | None, float | None, list[Opportunity]]:
    """Returns (YES basket cost, NO basket cost per $1 of payout, opportunities)."""
    yes = [tops.get(m.token_ids[0]) for m in group.markets]
    no = [tops.get(m.token_ids[1]) for m in group.markets]
    n = len(group.markets)
    opps: list[Opportunity] = []
    cy = cn = None
    if all(yes):
        cy = basket_cost([t[2] for t in yes], group.markets)
        if cy < 1:
            opps.append(Opportunity(group.title, "YES", cy, 1.0, min(t[3] for t in yes)))
    if all(no):
        cn = basket_cost([t[2] for t in no], group.markets)
        if cn < n - 1:
            opps.append(Opportunity(group.title, "NO", cn, n - 1.0, min(t[3] for t in no)))
        cn = cn - (n - 2)   # normalised so that "below 1.00" means profitable
    return cy, cn, opps


def scan(cfg, max_groups: int = 60) -> tuple[list[tuple], list[Opportunity]]:
    groups = load_groups(cfg)[:max_groups]
    tokens = [t for g in groups for m in g.markets for t in m.token_ids]
    tops = api.fetch_tops(tokens)
    rows, opps = [], []
    for g in groups:
        cy, cn, o = check(g, tops)
        rows.append((g, cy, cn))
        opps += o
    return rows, opps


def backtest(group: Group, tapes: list[list[tuple]], fresh_s: float) -> dict:
    """Replay past trades for one event.

    The best ask of an outcome is taken to be the price of the last trade
    where someone bought it; the best bid likewise from the last sale. A
    basket is only priced when EVERY leg traded within the last `fresh_s`
    seconds, so stale prices don't create imaginary profits. The size of an
    opportunity is the smallest of those trades (we know at least that much
    was available).
    """
    from .maker import normalize

    n = len(group.markets)
    events = sorted((ts, i, kind, p, size)
                    for i, rows in enumerate(tapes)
                    for ts, kind, p, size in normalize(rows, group.markets[i].tick))
    ask = [None] * n
    bid = [None] * n            # each entry: (price, ts, size)
    out = {"priced_yes": 0, "priced_no": 0, "best_yes": None, "best_no": None,
           "episodes": []}
    open_ep = {"YES": None, "NO": None}

    def evaluate(kind: str, quotes, ts: float) -> None:
        if any(q is None or ts - q[1] > fresh_s for q in quotes):
            cost = None
        else:
            prices = [q[0] if kind == "YES" else 1 - q[0] for q in quotes]
            cost = basket_cost(prices, group.markets) - (0 if kind == "YES" else n - 2)
            key = "priced_yes" if kind == "YES" else "priced_no"
            out[key] += 1
            b = "best_yes" if kind == "YES" else "best_no"
            out[b] = cost if out[b] is None else min(out[b], cost)
        ep = open_ep[kind]
        if cost is not None and cost < 1 - 1e-9:
            sets = min(q[2] for q in quotes)
            if ep is None:
                ep = open_ep[kind] = {"kind": kind, "start": ts, "end": ts,
                                      "edge": 1 - cost, "sets": sets}
                out["episodes"].append(ep)
            elif 1 - cost > ep["edge"]:
                ep["edge"], ep["sets"] = 1 - cost, sets
            ep["end"] = ts
        else:
            open_ep[kind] = None

    for ts, i, kind, p, size in events:
        if kind == "B":
            ask[i] = (p, ts, size)
            evaluate("YES", ask, ts)
        else:
            bid[i] = (p, ts, size)
            evaluate("NO", bid, ts)
    return out
