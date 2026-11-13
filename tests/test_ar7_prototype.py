"""AR-7 P0: basis capture, residual basis, costs, L* and the entry rule."""

import numpy as np
import pandas as pd

from ntx.experiments.ar7_prototype import (
    COST_TAKER,
    cycle_pnl,
    expiry_ts,
    first_entry,
    run_cycle,
    survival_leverage,
)


def test_cycle_pnl_is_basis_minus_residual_basis():
    # entry basis 5%; futures end 1 below spot -> residual -1% is credited
    assert abs(cycle_pnl(100, 105, 120, 119) - 0.06) < 1e-12
    # converged exactly: pure entry basis, whatever spot did
    assert abs(cycle_pnl(100, 105, 80, 80) - 0.05) < 1e-12


def test_taker_cost_is_45bps_at_1x():
    assert abs(COST_TAKER - 45e-4) < 1e-12


def test_survival_leverage_thresholds():
    lev, exc = survival_leverage(100, np.array([120.0]))
    assert lev == 3 and abs(exc - 0.2) < 1e-12
    assert survival_leverage(100, np.array([140.0]))[0] == 2  # 0.4*3 > 0.9, *2 = 0.8
    assert survival_leverage(100, np.array([160.0]))[0] == 1  # 0.6*2 > 0.9
    assert survival_leverage(100, np.array([200.0]))[0] == 0  # 1.0 > 0.9


def test_first_entry_needs_threshold_and_30_days():
    exp = expiry_ts("210625")
    idx = pd.date_range(exp - pd.Timedelta(days=40), periods=40 * 24, freq="1h", tz="UTC")
    s = np.full(len(idx), 100.0)
    f = np.full(len(idx), 100.0)
    f[10 * 24 :] = 100.0 * (1 + 0.10 * 20 / 365)  # 10% annualised from day 10 (30 days left)
    i = first_entry(idx, f, s, exp, 0.05, idx[0])
    assert i is not None and idx[i] >= exp - pd.Timedelta(days=30) - pd.Timedelta(hours=1)
    # basis arrives only inside the last 30 days -> never entered
    f2 = np.full(len(idx), 100.0)
    f2[15 * 24 :] = 110.0
    assert first_entry(idx, f2, s, exp, 0.05, idx[0]) is None


def test_run_cycle_net_and_excess():
    exp = expiry_ts("210625")
    idx = pd.date_range(exp - pd.Timedelta(days=50), exp, freq="1h", tz="UTC")[1:]
    n = len(idx)
    spot = pd.DataFrame({"high": 100.0, "close": 100.0}, index=idx)
    fut_close = np.full(n, 101.0)
    fut_close[-1] = 100.0  # converges exactly at expiry
    fut = pd.DataFrame({"high": fut_close * 1.05, "close": fut_close}, index=idx)
    c = run_cycle(fut, spot, exp, 0)
    assert abs(c["gross_bps"] - 100.0) < 1e-9  # 1% basis captured
    assert abs(c["net_taker2x_bps"] - (100.0 - 90.0)) < 1e-9  # 2 x 45 bps
    assert c["L_star"] == 3  # excursion ~5.9%
    days = c["days_held"]
    expected = (10e-4) / (1 + 1 / 3) - 0.04 * days / 365
    assert abs(c["excess"] - expected) < 1e-12
