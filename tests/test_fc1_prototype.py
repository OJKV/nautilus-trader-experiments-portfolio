"""FC-1 P0: a premium that converges earns its basis; the short perp receives funding."""

import pandas as pd

from ntx.experiments.fc1_prototype import simulate


def test_convergence_pays_the_entry_premium_and_funding_is_received():
    idx = pd.date_range("2021-01-01", periods=10, freq="1h", tz="UTC")
    p = pd.Series([0.0, 0.006, 0.004, 0.002, 0.0, 0.0, 0, 0, 0, 0], index=idx)
    f = pd.Series([0.0005], index=[idx[2]])  # settles while short: +5 bps received
    t = simulate(p, f, theta=0.005)
    assert len(t) == 1
    assert abs(t[0]["basis_bps"] - 60.0) < 1e-9
    assert abs(t[0]["funding_bps"] - 5.0) < 1e-9
    assert t[0]["hours"] == 3
