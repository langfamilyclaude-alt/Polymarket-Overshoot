from overshoot.config import Config
from overshoot.strategy import Detector, Engine, taker_fee


def feed(eng, series, start=0, **kw):
    out = []
    for i, p in enumerate(series):
        out += eng.on_quote("m", "test", start + i * 60, p - .005, p + .005, 1 - p - .005, 1 - p + .005, **kw)
    return out


def calm(n=60, p=0.50):
    return [p + (0.005 if i % 2 else 0) for i in range(n)]


def test_fee_peaks_at_half():
    assert abs(taker_fee(100, 0.5, 0.05, 1) - 1.25) < 1e-9
    assert taker_fee(100, 0.9, 0.05, 1) < taker_fee(100, 0.5, 0.05, 1)
    assert taker_fee(100, 0.5, 0, 1) == 0


def test_no_signal_when_calm():
    d = Detector(Config())
    assert all(d.update(i * 60, p) is None for i, p in enumerate(calm(120)))


def test_no_signal_before_spike_fades():
    d = Detector(Config())
    sigs = [d.update(i * 60, p) for i, p in enumerate(calm() + [0.55, 0.60])]
    assert not any(sigs)


def test_up_spike_then_revert_wins():
    eng = Engine(Config())
    log = feed(eng, calm() + [0.55, 0.60, 0.58, 0.56, 0.54, 0.52, 0.52])
    assert any(l.startswith("OPEN") and "side=1" in l for l in log)
    t = eng.trades[0]
    assert t.reason == "target" and t.pnl > 0


def test_down_spike_buys_outcome_zero():
    eng = Engine(Config())
    log = feed(eng, calm() + [0.45, 0.40, 0.43, 0.45, 0.47, 0.48])
    assert any("side=0" in l for l in log)
    assert eng.trades[0].pnl > 0


def test_spike_that_keeps_going_is_stopped():
    eng = Engine(Config())
    feed(eng, calm() + [0.55, 0.60, 0.58, 0.62, 0.66, 0.70])
    t = eng.trades[0]
    assert t.reason == "stop" and t.pnl < 0


def test_fees_can_block_a_trade():
    eng = Engine(Config())
    log = feed(eng, calm() + [0.55, 0.60, 0.58], fee_rate=0.5)
    assert not log


def test_daily_loss_limit_blocks_new_trades():
    cfg = Config(daily_loss_limit_usd=0.5, cooldown_s=0)
    eng = Engine(cfg)
    feed(eng, calm() + [0.55, 0.60, 0.58, 0.62, 0.66, 0.70])
    assert len(eng.trades) == 1
    feed(eng, calm(60, 0.70) + [0.75, 0.80, 0.78, 0.74], start=66 * 60)
    assert len(eng.trades) == 1 and not eng.positions


def test_one_position_per_event():
    eng = Engine(Config())
    series = calm() + [0.55, 0.60, 0.58]
    for i, p in enumerate(series):
        for key in ("a", "b"):
            eng.on_quote(key, key, i * 60, p - .005, p + .005, 1 - p - .005, 1 - p + .005, group="game1")
    assert len(eng.positions) == 1
