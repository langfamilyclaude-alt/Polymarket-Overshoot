"""Read-only client for Polymarket's public APIs (no account or keys needed)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com/v2"

_session = requests.Session()
_session.headers["User-Agent"] = "overshoot-scanner/0.1"


def _get(url: str, params: dict | None = None, retries: int = 3):
    for attempt in range(retries):
        try:
            r = _session.get(url, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


@dataclass
class Market:
    condition_id: str
    question: str
    slug: str
    outcomes: list[str]
    token_ids: list[str]          # token_ids[i] pays $1 if outcomes[i] wins
    best_bid: float               # for outcome 0
    best_ask: float
    volume_24h: float
    liquidity: float
    tick: float
    min_size: float
    fee_rate: float               # 0 when the market charges no fees
    fee_exponent: float
    rebate_rate: float            # share of taker fees paid back to makers
    neg_risk: bool
    event_id: str
    event_title: str
    game_start: datetime | None   # set for sports/esports fixtures
    end: datetime | None

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    def is_live(self, now: datetime | None = None) -> bool:
        """True when the underlying game has started and trading is open."""
        now = now or datetime.now(timezone.utc)
        return self.game_start is not None and self.game_start <= now


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace(" ", "T").replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_market(m: dict, default_rate: float, default_exp: float,
                 require_open: bool = True) -> Market | None:
    try:
        tokens = json.loads(m["clobTokenIds"])
        outcomes = json.loads(m["outcomes"])
        bid, ask = float(m.get("bestBid") or 0), float(m.get("bestAsk") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if len(tokens) != 2 or not m.get("enableOrderBook"):
        return None
    if require_open and (not m.get("acceptingOrders") or ask <= 0):
        return None
    if m.get("feesEnabled"):
        sched = m.get("feeSchedule") or {}
        rate = float(sched.get("rate", default_rate))
        exp = float(sched.get("exponent", default_exp))
        rebate = float(sched.get("rebateRate", 0))
    else:
        rate, exp, rebate = 0.0, 1.0, 0.0
    ev = (m.get("events") or [{}])[0]
    return Market(
        condition_id=m["conditionId"], question=m.get("question", ""), slug=m.get("slug", ""),
        outcomes=outcomes, token_ids=tokens, best_bid=bid, best_ask=ask,
        volume_24h=float(m.get("volume24hr") or 0), liquidity=float(m.get("liquidityNum") or 0),
        tick=float(m.get("orderPriceMinTickSize") or 0.01), min_size=float(m.get("orderMinSize") or 5),
        fee_rate=rate, fee_exponent=exp, rebate_rate=rebate, neg_risk=bool(m.get("negRisk")),
        event_id=str(ev.get("id", "")), event_title=ev.get("title", ""),
        game_start=_dt(m.get("gameStartTime")), end=_dt(m.get("endDate")),
    )


def fetch_markets(cfg, pages: int = 5, closed_since: datetime | None = None) -> list[Market]:
    """Markets, busiest first (Gamma returns at most 100 per page).

    By default only open markets. Pass closed_since to instead get markets that
    finished after that time (used by backtests so losers aren't left out).
    """
    out: list[Market] = []
    for page in range(pages):
        params = {"order": "volume24hr", "ascending": "false", "limit": 100, "offset": page * 100}
        if closed_since:
            params |= {"closed": "true", "end_date_min": closed_since.strftime("%Y-%m-%dT%H:%M:%SZ")}
        else:
            params |= {"active": "true", "closed": "false"}
        raw = _get(f"{GAMMA}/markets", params)
        if not raw:
            break
        for m in raw:
            pm = parse_market(m, cfg.default_fee_rate, cfg.default_fee_exponent,
                              require_open=closed_since is None)
            if pm:
                out.append(pm)
        if float(raw[-1].get("volume24hr") or 0) < cfg.min_volume_24h:
            break
    return out


def tradable(m: Market, cfg) -> bool:
    return (m.volume_24h >= cfg.min_volume_24h and m.liquidity >= cfg.min_liquidity
            and 0 < m.spread <= cfg.max_spread and cfg.min_price <= m.mid <= cfg.max_price)


def fetch_history(token_id: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
    """1-minute price history. The API caps each request, so fetch a day at a time."""
    pts: dict[int, float] = {}
    t = start_ts
    while t < end_ts:
        chunk_end = min(t + 86400, end_ts)
        data = _get(f"{CLOB}/prices-history",
                    {"market": token_id, "startTs": t, "endTs": chunk_end, "fidelity": 1})
        for row in (data or {}).get("history") or []:
            pts[int(row["t"])] = float(row["p"])
        t = chunk_end
    return sorted(pts.items())


def fetch_tops(token_ids: list[str]) -> dict[str, tuple[float, float, float, float]]:
    """token_id -> (best_bid, bid_size, best_ask, ask_size) for many tokens at once."""
    out = {}
    for i in range(0, len(token_ids), 100):
        batch = [{"token_id": t} for t in token_ids[i:i + 100]]
        r = _session.post(f"{CLOB}/books", json=batch, timeout=20)
        r.raise_for_status()
        for b in r.json():
            bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids") or []]
            asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks") or []]
            if not bids or not asks:
                continue
            bp, bs = max(bids)
            ap, asz = min(asks)
            out[b["asset_id"]] = (bp, bs, ap, asz)
    return out


def fetch_events(pages: int = 3) -> list[dict]:
    """Raw active events (each holds the full list of its markets)."""
    out = []
    for page in range(pages):
        raw = _get(f"{GAMMA}/events", {
            "active": "true", "closed": "false", "order": "volume24hr",
            "ascending": "false", "limit": 100, "offset": page * 100,
        })
        if not raw:
            break
        out.extend(raw)
    return out


def fetch_trades(condition_id: str, start_ts: int, max_pages: int = 400) -> tuple[list[tuple], bool]:
    """Every trade in a market since start_ts, oldest first.

    Returns (rows, complete). Each row is (timestamp, side, outcome_index,
    price, size), where side is what the taker did ("BUY"/"SELL"). complete is
    False if the page cap was hit before reaching start_ts.
    """
    rows, cursor = [], None
    for _ in range(max_pages):
        params = {"condition": condition_id, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        d = _get(f"{DATA}/trades", params)
        data = d.get("data") or []
        rows += [(int(x["timestamp"]), x["side"], int(x["outcome_index"]),
                  float(x["price"]), float(x["size"])) for x in data]
        page = d.get("pagination") or {}
        cursor = page.get("next_cursor")
        if not data or data[-1]["timestamp"] < start_ts or not page.get("has_more") or not cursor:
            rows = [r for r in rows if r[0] >= start_ts]
            rows.reverse()  # API is newest-first; reversing keeps same-second order
            return rows, True
    rows = [r for r in rows if r[0] >= start_ts]
    rows.reverse()
    return rows, False
