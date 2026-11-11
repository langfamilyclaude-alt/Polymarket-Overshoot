from overshoot.maker import MakerConfig, normalize, simulate

# A calm market: trades alternate at bid 0.50 / ask 0.51.
def calm(n=40, start=0, step=10):
    return [(start + i * step, "S" if i % 2 else "B", 0.50 if i % 2 else 0.51, 100.0) for i in range(n)]


def cfg(**kw):
    return MakerConfig(queue_usd=0, jump=1.0, **kw)


def test_normalize_maps_outcome_one_to_outcome_zero():
    ev = normalize([(1, "BUY", 0, 0.37, 5), (2, "BUY", 1, 0.64, 5), (3, "SELL", 1, 0.63, 5)], 0.01)
    assert ev[0][1:3] == ("B", 0.37)
    assert ev[1][1] == "S" and abs(ev[1][2] - 0.36) < 1e-9
    assert ev[2][1] == "B" and abs(ev[2][2] - 0.37) < 1e-9


def test_calm_market_earns_the_spread():
    r = simulate(calm(), cfg())
    assert r.fills > 10 and r.pnl > 0


def test_queue_ahead_reduces_fills():
    assert simulate(calm(), MakerConfig(queue_usd=1e9, jump=1.0)).fills == 0
    some = simulate(calm(200), MakerConfig(queue_usd=500, jump=1.0)).fills
    assert 0 < some < simulate(calm(200), cfg()).fills


def test_through_only_ignores_trades_at_our_price():
    assert simulate(calm(), cfg(through_only=True)).fills == 0


def test_price_collapse_loses_money():
    crash = [(400 + i, "S", 0.50 - 0.02 * i, 500.0) for i in range(1, 15)]
    r = simulate(calm() + crash, cfg())
    calm_only = simulate(calm(), cfg())
    assert r.pnl < calm_only.pnl and r.liquidation_cost > 0


def test_latency_delays_first_quote():
    ev = [(0, "B", 0.51, 100.0), (0, "S", 0.50, 100.0), (0, "S", 0.50, 100.0), (5, "S", 0.50, 100.0)]
    assert simulate(ev[:3], cfg(latency_s=1)).fills == 0
    assert simulate(ev, cfg(latency_s=1)).fills == 1


def test_inventory_cap_stops_buying():
    sells = [(0, "B", 0.51, 1.0)] + [(i * 10, "S", 0.50, 1000.0) for i in range(1, 60)]
    r = simulate(sells, cfg(max_inventory_usd=50, quote_usd=25))
    assert r.peak_inventory_usd <= 50 + 25


def test_pregame_phase_stops_before_game():
    r = simulate(calm(), cfg(phase="pregame", pregame_buffer_s=0), game_start=200)
    full = simulate(calm(), cfg())
    assert 0 < r.fills < full.fills
    assert simulate(calm(), cfg(phase="ingame"), game_start=10_000).fills == 0
