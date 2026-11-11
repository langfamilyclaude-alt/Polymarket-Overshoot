import numpy as np
import pandas as pd

from overshoot import btc_strategy as bs


def rows(n_windows=1, t=(120, 130), up=True, buy_up=0.50, buy_dn=0.51, z=0.0,
         late_up=None, late_dn=None):
    out = []
    for w in range(n_windows):
        for tt in t:
            out.append({"s": 86400 * 10 + w * 300, "t": tt, "up": up, "z": z,
                        "buy_up": buy_up, "buy_dn": buy_dn,
                        "buy_up_late": buy_up if late_up is None else late_up,
                        "buy_dn_late": buy_dn if late_dn is None else late_dn,
                        "flow": 0.0, "t15": 0.0, "t60": 0.0, "t240": 0.0, "t1440": 0.0})
    df = pd.DataFrame(out)
    df["mid"] = (df.buy_up + (1 - df.buy_dn)) / 2
    return df


W_UP = np.array([1.0, 1.0, 0.0])      # estimate ~73% Up when the market says 50%
W_FAIR = np.array([0.0, 1.0, 0.0])    # estimate == market price


def test_no_trade_when_estimate_matches_market():
    assert len(bs.simulate(rows(), W_FAIR, bs.BtcConfig())) == 0


def test_buys_up_once_per_window_and_pays_fee():
    tr = bs.simulate(rows(n_windows=3), W_UP, bs.BtcConfig(delayed=False))
    assert len(tr) == 3 and set(tr.side) == {"Up"} and (tr.t == 120).all()
    shares = 25 / 0.50
    fee = shares * 0.07 * 0.25
    assert abs(tr.pnl.iloc[0] - (shares - 25 - fee)) < 1e-9


def test_losing_trade_loses_stake_plus_fee():
    tr = bs.simulate(rows(up=False), W_UP, bs.BtcConfig(delayed=False))
    assert tr.pnl.iloc[0] < -25


def test_buys_down_when_estimate_is_low():
    tr = bs.simulate(rows(up=False), -W_UP, bs.BtcConfig(delayed=False))
    assert tr.side.iloc[0] == "Down" and tr.pnl.iloc[0] > 0


def test_delayed_fill_needs_price_still_available():
    cfg = bs.BtcConfig(delayed=True)
    gone = rows(late_up=0.90)                 # price ran away before our order arrived
    assert len(bs.simulate(gone, W_UP, cfg)) == 0
    missing = rows(late_up=float("nan"))      # nobody traded after us
    assert len(bs.simulate(missing, W_UP, cfg)) == 0
    worse = rows(late_up=0.55)                # filled, but at the later (worse) price
    assert abs(bs.simulate(worse, W_UP, cfg).price.iloc[0] - 0.55) < 1e-9


def test_respects_time_window_and_price_bounds():
    assert len(bs.simulate(rows(t=(30, 60)), W_UP, bs.BtcConfig(delayed=False))) == 0
    cheap = rows(buy_up=0.02, buy_dn=0.99)
    assert len(bs.simulate(cheap, np.array([3.0, 1.0, 0.0]), bs.BtcConfig(delayed=False))) == 0


def test_fit_learns_that_btc_move_matters():
    rng = np.random.default_rng(0)
    z = rng.normal(size=4000)
    up = rng.random(4000) < 1 / (1 + np.exp(-1.5 * z))
    df = pd.DataFrame({"s": np.arange(4000) * 300, "t": 120, "up": up, "z": z, "mid": 0.5,
                       "t15": 0.0, "t60": 0.0, "t240": 0.0, "t1440": 0.0, "flow": 0.0})
    w = bs.fit(df, bs.BtcConfig())
    assert 1.2 < w[2] < 1.8


def test_baseline_follows_the_feature_sign():
    df = rows(t=(10,), z=-1.0, up=False)
    tr = bs.baseline(df, "z", bs.BtcConfig())
    assert tr.side.iloc[0] == "Down" and tr.won.iloc[0]


def test_walk_forward_never_trades_the_training_days():
    df = pd.concat([rows().assign(s=86400 * d + 300) for d in range(10)], ignore_index=True)
    tr = bs.walk_forward(df, bs.BtcConfig(delayed=False), train_days=6)
    assert len(tr) == 0 or tr.s.min() >= 86400 * 6
