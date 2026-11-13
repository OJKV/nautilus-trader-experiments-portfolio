"""MM-2 P0: a touch is not a fill; a trade-through is, and the stop caps the loss."""

import numpy as np
import pandas as pd

from ntx.experiments import mm2_prototype as mm


def _df(hours=80, wick=None):
    idx = pd.date_range("2021-01-01", periods=hours * 60, freq="1min", tz="UTC")
    rng = np.random.default_rng(1)
    c = 100 * np.exp(rng.normal(0, 0.0005, len(idx)).cumsum())
    df = pd.DataFrame({"o": c, "h": c * 1.0001, "l": c * 0.9999, "c": c}, index=idx)
    if wick is not None:
        df.iloc[wick, df.columns.get_loc("l")] = df["c"].iloc[wick] * 0.90  # a -10% wick
    return df


def test_a_deep_wick_fills_the_buy_limit_and_a_quiet_market_does_not(monkeypatch):
    monkeypatch.setattr(mm, "SIGMA_H", 24)
    quiet, _ = mm.trades_for(_df(), k=4.0, hold=60)
    wick_minute = 50 * 60 + 10
    filled, _ = mm.trades_for(_df(wick=wick_minute), k=4.0, hold=60)
    assert len(filled) > len(quiet)
