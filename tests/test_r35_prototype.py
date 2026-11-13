"""R3-5 P0: trades follow the flow sign, never overlap, and longs pay funding."""

import numpy as np
import pandas as pd

from ntx.experiments.r35_prototype import trades_for


def test_continuation_sign_hold_and_funding():
    idx = pd.date_range("2021-01-01 04:00", periods=8, freq="4h", tz="UTC")
    bars = pd.DataFrame({"c": [100, 101, 102, 103, 104, 105, 106, 107.0]}, index=idx)
    z = pd.Series([3.0, 3.0, 3.0, 0, 0, 0, 0, 0], index=idx)  # strong buying flow
    fund = pd.Series([0.001], index=[pd.Timestamp("2021-01-01 16:00", tz="UTC")])
    t = trades_for(bars, z, fund, zt=2.0, hold=3, sign=+1)
    assert len(t) == 1, "signals while holding are ignored"
    assert abs(t[0]["gross"] - 300.0) < 1e-9  # long 100 -> 103
    assert abs(t[0]["fund"] + 10.0) < 1e-9  # the long PAYS 10 bps
    ctrl = trades_for(bars, z, fund, zt=2.0, hold=3, sign=-1)
    assert ctrl[0]["gross"] < 0 and np.isclose(ctrl[0]["fund"], 10.0)
