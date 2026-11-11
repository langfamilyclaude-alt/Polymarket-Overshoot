from overshoot import api, arb


def mk(i, fee=0.0):
    return api.Market(f"c{i}", f"q{i}", "", ["Yes", "No"], [f"y{i}", f"n{i}"], 0, 0, 0, 0, 0.01, 5,
                      fee, 1.0, 0.0, True, "", "", None, None)


def tops(yes_asks, size=100.0):
    t = {}
    for i, a in enumerate(yes_asks):
        t[f"y{i}"] = (a - 0.01, size, a, size)
        t[f"n{i}"] = (1 - a, size, 1 - a + 0.01, size)
    return t


def test_fair_prices_show_no_opportunity():
    g = arb.Group("e", [mk(0), mk(1), mk(2)], 0)
    cy, cn, opps = arb.check(g, tops([0.50, 0.30, 0.21]))
    assert cy > 1 and cn > 1 and not opps


def test_cheap_yes_basket_is_found_and_sized_by_thinnest_leg():
    g = arb.Group("e", [mk(0), mk(1), mk(2)], 0)
    t = tops([0.50, 0.30, 0.15])
    t["y2"] = (0.14, 100.0, 0.15, 20.0)
    cy, _, opps = arb.check(g, t)
    assert abs(cy - 0.95) < 1e-9
    assert opps[0].kind == "YES" and opps[0].sets == 20 and abs(opps[0].profit - 1.0) < 1e-9


def test_cheap_no_basket_is_found():
    g = arb.Group("e", [mk(0), mk(1), mk(2)], 0)
    t = tops([0.60, 0.40, 0.20])          # YES bids sum to 1.17 -> NO asks sum to 1.83 < 2
    _, cn, opps = arb.check(g, t)
    assert cn < 1 and [o.kind for o in opps] == ["NO"]


def test_fees_remove_a_thin_edge():
    free = arb.Group("e", [mk(0), mk(1)], 0)
    paid = arb.Group("e", [mk(0, 0.05), mk(1, 0.05)], 0)
    t = tops([0.50, 0.49])
    assert arb.check(free, t)[2] and not arb.check(paid, t)[2]


def test_missing_book_means_no_yes_basket():
    g = arb.Group("e", [mk(0), mk(1)], 0)
    t = tops([0.4, 0.4])
    del t["y1"]
    cy, _, opps = arb.check(g, t)
    assert cy is None and all(o.kind != "YES" for o in opps)


def test_backtest_needs_every_leg_fresh():
    g = arb.Group("e", [mk(0), mk(1)], 0)
    # leg 0 bought at 0.40 at t=0; leg 1 bought at 0.50 at t=10 -> basket 0.90
    tapes = [[(0, "BUY", 0, 0.40, 50.0)], [(10, "BUY", 0, 0.50, 30.0)]]
    r = arb.backtest(g, tapes, fresh_s=60)
    assert len(r["episodes"]) == 1
    e = r["episodes"][0]
    assert abs(e["edge"] - 0.10) < 1e-9 and e["sets"] == 30
    assert not arb.backtest(g, tapes, fresh_s=5)["episodes"]


def test_backtest_fair_prices_no_episode():
    g = arb.Group("e", [mk(0), mk(1)], 0)
    tapes = [[(0, "BUY", 0, 0.52, 50.0)], [(1, "BUY", 0, 0.50, 30.0)]]
    assert not arb.backtest(g, tapes, 60)["episodes"]
