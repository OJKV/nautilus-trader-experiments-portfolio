"""R3-3 P0: a trending ratio earns, flips cost 4 fills, the first entry 2."""

import numpy as np
import pandas as pd

from ntx.experiments.r33_prototype import simulate


def test_a_rising_ratio_is_held_long_and_costs_one_entry():
    idx = pd.date_range("2021-01-01", periods=200, freq="1D", tz="UTC")
    eth = pd.Series(100 * np.exp(np.linspace(0, 0.5, 200)), index=idx)  # ETH outperforms
    btc = pd.Series(100.0, index=idx)
    zero = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    daily, flips = simulate(eth, btc, zero, zero, n=30, band_k=0.0, fill_bps=10.0)
    assert flips == 0
    assert daily.sum() > 0
    assert abs(daily.iloc[30] + 20.0) < 1e-6  # the entry day pays 2 fills
