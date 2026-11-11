"""All tunable settings live here so they are easy to find and change."""
from dataclasses import dataclass


@dataclass
class Config:
    # ---- which markets to watch -------------------------------------------
    min_volume_24h: float = 20_000    # USD traded in the last 24h
    min_liquidity: float = 5_000      # USD resting in the book
    max_spread: float = 0.03          # skip markets wider than 3 cents
    min_price: float = 0.10           # skip near-certain markets: tiny moves,
    max_price: float = 0.90           # and one bad resolution wipes many wins
    max_markets: int = 40

    # ---- overshoot detection ----------------------------------------------
    lookback_s: int = 300             # a "spike" is a move inside this window
    baseline_s: int = 3 * 3600        # normal volatility is measured over this
    min_baseline_s: int = 1800        # need this much history before trading
    z_enter: float = 4.0              # spike must be this many "normal moves" big
    min_spike: float = 0.04           # ...and at least 4 cents
    vol_floor: float = 0.004          # never treat volatility as smaller than this
    confirm_frac: float = 0.20        # wait until 20% of the spike has faded back
    max_retrace_frac: float = 0.45    # ...but skip if it has mostly faded already

    # ---- trade management -------------------------------------------------
    revert_frac: float = 0.55         # take profit once 55% of the spike is undone
    stop_frac: float = 0.35           # cut the loss if it runs 35% past the peak
    max_hold_s: int = 20 * 60         # exit after 20 minutes no matter what
    min_edge: float = 0.01            # need >= 1 cent/share profit after costs
    cooldown_s: int = 15 * 60         # rest a market after trading it

    # ---- risk limits ------------------------------------------------------
    stake_usd: float = 25.0           # dollars per trade
    max_open_positions: int = 5
    daily_loss_limit_usd: float = 50.0  # stop opening trades after losing this

    # ---- simulation assumptions ------------------------------------------
    default_fee_rate: float = 0.05    # used when a market reports no schedule
    default_fee_exponent: float = 1.0
    backtest_spread: float = 0.01     # assumed bid/ask spread in backtests
    poll_s: float = 5.0               # live polling interval
