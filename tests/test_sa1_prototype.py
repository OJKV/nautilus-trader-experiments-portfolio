"""SA-1 P0: pair P&L and funding signs must be right, or the verdict means nothing."""

import numpy as np
import pandas as pd

from ntx.experiments.sa1_prototype import simulate


def test_a_short_eth_long_btc_trade_books_the_reversion_and_receives_eth_funding():
    idx = pd.date_range("2021-01-01", periods=60, freq="1h", tz="UTC")
    rng = np.random.default_rng(0)
    eth = pd.Series(100 * np.exp(rng.normal(0, 0.001, 60).cumsum()), index=idx)
    btc = pd.Series(100.0, index=idx)
    eth.iloc[40] = eth.iloc[39] * 1.10  # ETH spikes: z >> 2 -> short ETH / long BTC
    eth.iloc[41:] = eth.iloc[39]  # and reverts
    f_eth = pd.Series(0.001, index=idx[[41]])  # longs pay 10 bps at the hour after entry
    f_btc = pd.Series(0.0, index=idx[[41]])
    t = simulate(eth, btc, f_eth, f_btc, lookback=24, entry_z=2.0)
    assert len(t) >= 1
    first = t.iloc[0]
    assert first["side"] == -1
    assert first["gross_bps"] > 0, "shorting the spike and covering at the reversion earns"
    assert abs(first["funding_bps"] - 10.0) < 1e-6, "the short ETH leg RECEIVES funding"
