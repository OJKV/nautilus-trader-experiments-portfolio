"""AR-4 P0: per-hour normalisation, no look-ahead, funding sign, price leg, exits, costs."""

import numpy as np
import pandas as pd
import pytest

from ntx.experiments import ar4_prototype as a

T0 = pd.Timestamp("2022-01-01", tz="UTC")


def settle(hours: list[float], rates: list[float]) -> pd.Series:
    return pd.Series(rates, index=[T0 + pd.Timedelta(hours=h) for h in hours])


def synth(n_days: int, ra, rb, pa=None, pb=None, sym="TEST") -> a.Pair:
    """8h settlements on both venues; rates ra(day_index_of_settlement) etc.; flat prices."""
    n_set = n_days * 3
    hrs = [8.0 * i for i in range(n_set + 1)]
    fa = settle(hrs, [ra(h) for h in hrs])
    fb = settle(hrs, [rb(h) for h in hrs])
    px = pd.DatetimeIndex([T0 + pd.Timedelta(hours=h) for h in range(n_days * 24 + 1)])
    pav = pd.Series(100.0 if pa is None else [pa(h) for h in range(len(px))], index=px)
    pbv = pd.Series(100.0 if pb is None else [pb(h) for h in range(len(px))], index=px)
    return a.make_pair(sym, fa, fb, pav, pbv)


def test_per_hour_mixed_intervals_and_missing_rows():
    f = settle([0, 8, 12, 14, 15, 24], [0.0008, 0.0008, 0.0004, 0.0002, 0.0001, 0.0009])
    ph = a.per_hour(f)
    assert np.isnan(ph.iloc[0])  # first row: no spacing
    assert np.isclose(ph.iloc[1], 0.0001)  # 8h spacing
    assert np.isclose(ph.iloc[2], 0.0001)  # 4h spacing: 0.0004/4
    assert np.isclose(ph.iloc[3], 0.0001)  # 2h
    assert np.isclose(ph.iloc[4], 0.0001)  # 1h
    assert np.isnan(ph.iloc[5])  # 9h spacing = a missing row, never a silently halved rate


def test_step_hourly_uses_last_settlement_only_and_expires():
    ph = settle([8, 16], [0.0001, 0.0002])
    grid = pd.DatetimeIndex([T0 + pd.Timedelta(hours=h) for h in (7, 8, 15, 16, 24, 25)])
    v = a.step_hourly(ph, grid)
    assert np.isnan(v[0])  # before the first settlement
    assert v[1] == 0.0001 and v[2] == 0.0001  # held forward, not interpolated
    assert v[3] == 0.0002 and v[4] == 0.0002  # age exactly 8h still valid
    assert np.isnan(v[5])  # older than 8h: stale


def test_forecast_has_no_lookahead():
    base = synth(15, lambda h: 0.0003, lambda h: 0.0001)
    changed = synth(15, lambda h: 0.0003 if h < 200 else -0.0009, lambda h: 0.0001)
    k = int(np.searchsorted(base.sec, int((T0 + pd.Timedelta(hours=199)).timestamp())))
    assert np.isfinite(base.shat[k])
    assert base.shat[k] == changed.shat[k]  # the future (h >= 200) cannot move it
    assert not np.isclose(base.shat[-1], changed.shat[-1])


def test_forecast_value_is_72h_trailing_mean_in_8h_equivalent_bps():
    p = synth(10, lambda h: 0.0003, lambda h: 0.0001)
    # per-hour diff = 0.0002/8 ; x8 x1e4 = 2 bps per 8h-equivalent, constant
    k = int(np.searchsorted(p.sec, int((T0 + pd.Timedelta(hours=100)).timestamp())))
    assert np.isclose(p.s[k], 2.0) and np.isclose(p.shat[k], 2.0)


def test_funding_sign_short_high_long_low_and_window_edges():
    p = synth(10, lambda h: 0.0003, lambda h: 0.0001)
    k0 = int(np.searchsorted(p.sec, int((T0 + pd.Timedelta(hours=80)).timestamp())))
    k1 = int(np.searchsorted(p.sec, int((T0 + pd.Timedelta(hours=104)).timestamp())))
    # settlements strictly after 80h, up to and including 104h: 88, 96, 104 -> 3 of them
    short_a = a.trade_pnl(p, +1, k0, k1)
    assert np.isclose(short_a["funding"], 3 * (0.0003 - 0.0001) * 1e4)  # +6 bps: short A receives
    long_a = a.trade_pnl(p, -1, k0, k1)
    assert np.isclose(long_a["funding"], -short_a["funding"])  # the long of the high rate pays
    assert short_a["hours"] == 24.0


def test_price_difference_leg():
    # B rises 1% while A is flat: long B / short A (pos=+1) gains 100 bps; the reverse loses
    p = synth(10, lambda h: 0.0001, lambda h: 0.0001,
              pb=lambda h: 100.0 if h < 90 else 101.0)  # fmt: skip
    k0 = int(np.searchsorted(p.sec, int((T0 + pd.Timedelta(hours=80)).timestamp())))
    k1 = int(np.searchsorted(p.sec, int((T0 + pd.Timedelta(hours=96)).timestamp())))
    assert np.isclose(a.trade_pnl(p, +1, k0, k1)["price"], 100.0)
    assert np.isclose(a.trade_pnl(p, -1, k0, k1)["price"], -100.0)


def test_costs_levels_and_doubling():
    assert a.cost_bps("maker", 1.0) == 8.0 and a.cost_bps("maker", 2.0) == 16.0
    assert a.cost_bps("taker", 1.0) == 20.0 and a.cost_bps("taker+slip", 2.0) == 80.0
    rows = [{"price": 5.0, "funding": 30.0, "year": 2023, "hours": 24.0}]
    assert np.isclose(a.net(rows, "maker", 2.0)[0], 5 + 30 - 16)
    assert np.isclose(a.net(rows, "taker+slip", 1.0)[0], 5 + 30 - 40)


def test_entry_exit_on_threshold_and_no_same_bar_reentry():
    # diff of 2 bps/8h until hour 192, then none: enters once the 72h forecast is warm, exits
    # when the forecast decays under theta/2 = 0.5
    p = synth(40, lambda h: 0.0003, lambda h: 0.0001 if h < 192 else 0.0003)
    lo, hi = T0, T0 + pd.Timedelta(days=40)
    rows, _ = a.simulate(p, 1.0, lo, hi)
    assert len(rows) == 1 and rows[0]["pos"] == 1  # short A, the higher-funding venue
    r = rows[0]
    assert r["t_in"] >= T0 + pd.Timedelta(hours=72)  # not before the 72h window exists
    assert r["reason"] == "revert"
    assert abs(p.shat[r["k_out"]]) < 0.5  # theta / 2
    prev = p.dec[np.searchsorted(p.dec, r["k_out"]) - 1]  # the decision time before the exit
    assert abs(p.shat[prev]) >= 0.5  # it exited at the FIRST decision time under the bar
    assert T0 + pd.Timedelta(hours=192) < r["t_out"] <= T0 + pd.Timedelta(hours=192 + 72)


def test_time_stop_at_14_days_then_reentry_only_on_a_later_bar():
    p = synth(60, lambda h: 0.0003, lambda h: 0.0001)  # permanent 2 bps/8h
    rows, _ = a.simulate(p, 1.0, T0, T0 + pd.Timedelta(days=60))
    assert len(rows) >= 2
    assert all(r["reason"] == "time" for r in rows[:2])
    assert rows[0]["hours"] == 14 * 24
    assert rows[1]["t_in"] > rows[0]["t_out"]  # never re-entered on the exit bar


def test_below_theta_never_enters_and_theta_is_inclusive():
    p = synth(20, lambda h: 0.00019, lambda h: 0.0001)  # 0.9 bps/8h
    assert a.simulate(p, 1.0, T0, T0 + pd.Timedelta(days=20))[0] == []
    q = synth(20, lambda h: 0.0002, lambda h: 0.0001)  # exactly 1.0 bps/8h
    assert len(a.simulate(q, 1.0, T0, T0 + pd.Timedelta(days=20))[0]) >= 1


def test_disagreeing_signs_do_not_enter():
    # forecast strongly positive, but the latest settlement flips negative
    def ra(h):
        return 0.0003

    def rb(h):
        return 0.0001 if h < 190 else 0.0009  # from h=192 s is -6 bps; shat still > 1 for a while

    p = synth(10, ra, rb)
    k = int(np.searchsorted(p.sec, int((T0 + pd.Timedelta(hours=192)).timestamp())))
    assert p.shat[k] > 1.0 and p.s[k] < -1.0
    rows, _ = a.simulate(p, 1.0, T0 + pd.Timedelta(hours=190), T0 + pd.Timedelta(hours=192))
    assert rows == []


def test_zero_data_raises_not_zero_trades():
    empty = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    ok = settle([0, 8, 16], [0.0001] * 3)
    px = pd.Series([1.0, 1.0], index=pd.DatetimeIndex([T0, T0 + pd.Timedelta(hours=1)]))
    with pytest.raises(RuntimeError, match="R35"):
        a.make_pair("X", empty, ok, px, px)
    with pytest.raises(RuntimeError, match="R35"):
        a.make_pair("X", ok, ok, empty, px)


def test_robust_rows_drop_ftx_window_and_top_days():
    t = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
    rows = [
        {"sym": "A", "t_in": t("2022-11-05"), "t_out": t("2022-11-07"), "price": 0, "funding": 9},
        {"sym": "A", "t_in": t("2022-12-01"), "t_out": t("2022-12-03"), "price": 0, "funding": 9},
        {"sym": "A", "t_in": t("2023-01-01"), "t_out": t("2023-01-03"), "price": 0, "funding": 9},
    ]
    drops = {"thr": 5.0, "days": {("A", t("2022-12-02"))}}
    kept = a.robust_rows(rows, drops)
    assert [r["t_in"] for r in kept] == [t("2023-01-01")]  # FTX overlap and top-day overlap out


def test_static_control_side_follows_trailing_30d_mean_and_random_is_seeded():
    p = synth(50, lambda h: 0.0003, lambda h: 0.0001)
    rows, _ = a.simulate(p, 1.0, T0, T0 + pd.Timedelta(days=50))
    sc, matched = a.static_control(rows, {"TEST": p})
    for r, c in zip(matched, sc, strict=True):
        # persistent s>0 -> the 30d mean says short A, i.e. the same side the rule took
        assert np.isclose(c["funding"], r["funding"])
    r1 = a.random_control(rows, {"TEST": p}, T0, T0 + pd.Timedelta(days=50))
    r2 = a.random_control(rows, {"TEST": p}, T0, T0 + pd.Timedelta(days=50))
    assert len(r1) == a.REPS
    assert [x["funding"] for x in r1[0]] == [x["funding"] for x in r2[0]]
