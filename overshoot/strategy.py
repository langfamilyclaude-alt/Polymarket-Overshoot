"""Overshoot detection and the simulated ("paper") trading engine.

The same engine is used for backtests and for live paper trading, so results
from one are comparable with the other.

Idea: when a price jumps far more than it normally moves in a few minutes and
then starts to drift back, bet on it drifting back further. Polymarket has no
short selling, so "betting against" outcome A means buying outcome B.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .config import Config


def taker_fee(shares: float, price: float, rate: float, exponent: float) -> float:
    """Fee in USD for taking liquidity. Resting (maker) orders pay nothing.

    Formula: shares * rate * (p * (1 - p)) ** exponent. This is my reading of
    Polymarket's fee schedule and is NOT verified against a real fill; check it
    against an actual trade confirmation before relying on the numbers.
    """
    if rate <= 0:
        return 0.0
    return shares * rate * (price * (1 - price)) ** exponent


@dataclass
class Signal:
    side: int          # 0 = buy outcome 0 (price fell too far), 1 = buy outcome 1
    spike: float       # size of the move, in price of outcome 0 (always > 0)
    z: float           # how many "normal moves" big the spike was
    extreme: float     # the peak/trough mid price of outcome 0
    target_mid: float  # outcome-0 mid at which we take profit
    stop_mid: float    # outcome-0 mid at which we give up


class Detector:
    """Tracks one market's mid price and flags spikes that have begun to fade."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pts: deque[tuple[float, float]] = deque()  # (timestamp, mid of outcome 0)

    def update(self, ts: float, mid: float) -> Signal | None:
        c = self.cfg
        if self.pts and ts <= self.pts[-1][0]:
            return None
        self.pts.append((ts, mid))
        while self.pts[0][0] < ts - c.baseline_s - c.lookback_s:
            self.pts.popleft()
        win_start = ts - c.lookback_s
        if win_start - self.pts[0][0] < c.min_baseline_s:
            return None

        # Normal volatility, measured only on data BEFORE the window so the
        # spike itself doesn't make spikes look normal.
        sq = dt = 0.0
        ref = None
        prev = None
        hi = lo = mid
        for t, p in self.pts:
            if t <= win_start:
                if prev is not None:
                    sq += (p - prev[1]) ** 2
                    dt += t - prev[0]
                ref = p
            else:
                hi, lo = max(hi, p), min(lo, p)
            prev = (t, p)
        if ref is None or dt <= 0:
            return None
        sigma = max(math.sqrt(sq / dt * c.lookback_s), c.vol_floor)

        up, down = hi - ref, ref - lo
        if up >= down:
            spike, retrace, side, extreme = up, hi - mid, 1, hi
        else:
            spike, retrace, side, extreme = down, mid - lo, 0, lo
        z = spike / sigma
        if spike < c.min_spike or z < c.z_enter:
            return None
        if not (c.confirm_frac * spike <= retrace <= c.max_retrace_frac * spike):
            return None
        sign = 1 if side == 1 else -1  # +1: outcome-0 price spiked UP
        return Signal(side=side, spike=spike, z=z, extreme=extreme,
                      target_mid=extreme - sign * c.revert_frac * spike,
                      stop_mid=extreme + sign * c.stop_frac * spike)


@dataclass
class Position:
    key: str
    label: str
    side: int
    shares: float
    entry_px: float
    entry_fee: float
    entry_ts: float
    tp_px: float       # resting take-profit price, in the token we hold
    stop_px: float     # stop level on the held token's mid
    z: float
    fee_rate: float
    fee_exp: float
    group: str = ""


@dataclass
class Trade:
    key: str
    label: str
    side: int
    shares: float
    entry_ts: float
    exit_ts: float
    entry_px: float
    exit_px: float
    fees: float
    pnl: float
    reason: str        # "target", "stop", "time" or "end"
    z: float


@dataclass
class Engine:
    """Decides entries/exits and keeps a simulated account."""
    cfg: Config
    detectors: dict[str, Detector] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    cooldown_until: dict[str, float] = field(default_factory=dict)
    day_pnl: dict[int, float] = field(default_factory=dict)

    def on_quote(self, key: str, label: str, ts: float,
                 bid0: float, ask0: float, bid1: float, ask1: float,
                 tick: float = 0.01, fee_rate: float = 0.0, fee_exp: float = 1.0,
                 min_size: float = 5.0, group: str = "") -> list[str]:
        """Feed one quote (best bid/ask of both outcomes). Returns log lines."""
        c = self.cfg
        events: list[str] = []
        mid0 = (bid0 + ask0) / 2
        det = self.detectors.setdefault(key, Detector(c))
        sig = det.update(ts, mid0)

        pos = self.positions.get(key)
        if pos:
            bid, ask = (bid0, ask0) if pos.side == 0 else (bid1, ask1)
            mid = (bid + ask) / 2
            if bid >= pos.tp_px:
                # Our resting sell was reached. Assume we get only our limit
                # price (conservative) and pay no fee, as a maker.
                events.append(self._close(pos, ts, pos.tp_px, "target", maker=True))
            elif mid <= pos.stop_px:
                events.append(self._close(pos, ts, bid, "stop"))
            elif ts - pos.entry_ts >= c.max_hold_s:
                events.append(self._close(pos, ts, bid, "time"))
            return events

        if not sig or ts < self.cooldown_until.get(key, 0):
            return events
        if len(self.positions) >= c.max_open_positions:
            return events
        # One bet per event: markets on the same game move together, so
        # several positions there are really one big position.
        if group and any(p.group == group for p in self.positions.values()):
            return events
        if self.day_pnl.get(int(ts // 86400), 0.0) <= -c.daily_loss_limit_usd:
            return events
        if not (c.min_price <= mid0 <= c.max_price) or ask0 - bid0 > c.max_spread:
            return events

        if sig.side == 0:
            bid, ask, tp_mid, stop_mid = bid0, ask0, sig.target_mid, sig.stop_mid
        else:
            bid, ask, tp_mid, stop_mid = bid1, ask1, 1 - sig.target_mid, 1 - sig.stop_mid
        tp_px = math.floor(tp_mid / tick + 1e-9) * tick
        fee_per_share = taker_fee(1.0, ask, fee_rate, fee_exp)
        edge = tp_px - ask - fee_per_share  # profit/share if the target is hit
        if edge < c.min_edge:
            return events
        shares = math.floor(c.stake_usd / ask)
        if shares < min_size:
            return events
        pos = Position(key=key, label=label, side=sig.side, shares=shares, entry_px=ask,
                       entry_fee=fee_per_share * shares, entry_ts=ts, tp_px=tp_px,
                       stop_px=stop_mid, z=sig.z, fee_rate=fee_rate, fee_exp=fee_exp, group=group)
        self.positions[key] = pos
        events.append(f"OPEN  {label[:48]:48} side={sig.side} {shares:.0f} @ {ask:.3f} "
                      f"spike={sig.spike:.3f} z={sig.z:.1f} target={tp_px:.3f} stop={stop_mid:.3f}")
        return events

    def _close(self, pos: Position, ts: float, px: float, reason: str, maker: bool = False) -> str:
        exit_fee = 0.0 if maker else taker_fee(pos.shares, px, pos.fee_rate, pos.fee_exp)
        fees = pos.entry_fee + exit_fee
        pnl = (px - pos.entry_px) * pos.shares - fees
        self.trades.append(Trade(pos.key, pos.label, pos.side, pos.shares, pos.entry_ts, ts,
                                 pos.entry_px, px, fees, pnl, reason, pos.z))
        del self.positions[pos.key]
        self.cooldown_until[pos.key] = ts + self.cfg.cooldown_s
        day = int(ts // 86400)
        self.day_pnl[day] = self.day_pnl.get(day, 0.0) + pnl
        return (f"CLOSE {pos.label[:48]:48} {reason:6} @ {px:.3f} "
                f"pnl=${pnl:+.2f} (fees ${fees:.2f}) held {int(ts - pos.entry_ts)}s")

    def close_all(self, ts: float, bids: dict[str, tuple[float, float]]) -> None:
        """Close whatever is still open (end of a backtest)."""
        for key, pos in list(self.positions.items()):
            if key in bids:
                self._close(pos, ts, bids[key][pos.side], "end")


def summarize(trades: list[Trade]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0}
    wins = [t for t in trades if t.pnl > 0]
    pnl = sum(t.pnl for t in trades)
    fees = sum(t.fees for t in trades)
    staked = sum(t.entry_px * t.shares for t in trades)
    by_reason: dict[str, list[float]] = {}
    for t in trades:
        by_reason.setdefault(t.reason, []).append(t.pnl)
    return {
        "trades": n,
        "win_rate": len(wins) / n,
        "net_pnl": pnl,
        "fees_paid": fees,
        "pnl_before_fees": pnl + fees,
        "avg_pnl_per_trade": pnl / n,
        "return_on_staked": pnl / staked if staked else 0.0,
        "avg_win": sum(t.pnl for t in wins) / len(wins) if wins else 0.0,
        "avg_loss": (pnl - sum(t.pnl for t in wins)) / (n - len(wins)) if n > len(wins) else 0.0,
        "by_exit": {k: (len(v), sum(v)) for k, v in sorted(by_reason.items())},
    }
