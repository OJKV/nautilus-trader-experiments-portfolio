"""NX-2 P0: p0 must be the 4h close and p_k the close k minutes later, signed per side."""

import pandas as pd

from ntx.experiments.nx2_prototype import delay_costs


def test_delay_cost_signs_and_alignment():
    idx = pd.date_range("2021-01-01 03:50", "2021-01-01 04:20", freq="1min", tz="UTC")
    bars = pd.DataFrame({"open": 100.0, "close": 100.0}, index=idx)
    bars.loc["2021-01-01 03:59", "close"] = 100.0  # the 04:00 bar close (p0)
    bars.loc["2021-01-01 04:00", "close"] = 101.0  # close at 04:01 (k=1)
    ts = pd.Timestamp("2021-01-01 04:00", tz="UTC")
    entries = pd.DataFrame({"ts": [ts, ts], "entry": ["BUY", "SELL"]})
    c = delay_costs(bars, entries)
    assert abs(c.loc[0, 1] - 100.0) < 1e-9  # buying 1% higher costs +100 bps
    assert c.loc[1, 1] < 0  # selling higher is cheaper for a short
