"""CS-1 P0: shorts the highest-funding perp, which then RECEIVES funding."""

import pandas as pd

from ntx.experiments.cs1_prototype import weekly_book


def test_the_high_funding_perp_is_shorted_and_its_funding_is_received():
    idx = pd.date_range("2021-01-04", periods=24 * 15, freq="1h", tz="UTC")  # Monday start
    closes = pd.DataFrame({"HI": 100.0, "LO": 100.0}, index=idx)
    fund = pd.DataFrame({"HI": 0.0, "LO": 0.0}, index=idx)
    fund.loc[idx[: 24 * 7], "HI"] = 0.001  # high funding during week 1 -> ranked for week 2
    fund.loc[idx[24 * 7 + 8], "HI"] = 0.002  # settles during week 2 while we are short
    gross, turnover = weekly_book(closes, fund, n=1, lookback_d=7)
    wk2 = gross.iloc[-1]
    # short HI receives 20 bps at weight 1/2 -> +10 bps; prices flat
    assert abs(wk2 - 10.0) < 1e-9
    assert turnover.iloc[0] > 0
