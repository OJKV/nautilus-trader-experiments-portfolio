"""SE-1 P0: the window must be entered at 22:00 open and left at 23:59 close, no lookahead."""

import pandas as pd

from ntx.experiments.se1_prototype import window_returns


def test_window_return_uses_the_22_open_and_the_2359_close():
    idx = pd.date_range("2021-01-01 21:00", "2021-01-02 01:00", freq="1min", tz="UTC")
    bars = pd.DataFrame({"open": 100.0, "close": 100.0}, index=idx)
    bars.loc["2021-01-01 22:00", "open"] = 100.0
    bars.loc["2021-01-01 23:59", "close"] = 101.0
    bars.loc["2021-01-02 00:00", "close"] = 200.0  # after the exit: must be ignored
    r = window_returns(bars, 22)
    assert len(r) == 1
    assert abs(r.iloc[0] - 100.0) < 1e-9  # +1% = 100 bps
