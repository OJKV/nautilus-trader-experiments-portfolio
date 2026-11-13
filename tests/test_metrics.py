"""Metrics tests.

Monitoring code gets the same scrutiny as trading code (pattern 12): a metrics
bug produces authoritative-looking wrong numbers, and the go-live gate is
written against these figures.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ntx.backtest.metrics import PERIODS_PER_YEAR, compute, money_to_float


def _positions(pnls: list[float], fees: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "realized_pnl": [f"{p:.8f} USDT" for p in pnls],
            "commissions": [f"[{fees:.8f} USDT]" for _ in pnls],
            "duration_ns": [3_600_000_000_000 for _ in pnls],
        }
    )


def _equity(values: list[float], freq: str = "1h") -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=len(values), freq=freq, tz="UTC")
    return pd.Series(values, index=idx, name="equity")


# ----------------------------- money parsing -------------------------------- #
def test_money_to_float_parses_nautilus_strings():
    """`pd.to_numeric` returns all-NaN on these, which silently blanked the
    runner's own summary."""
    s = pd.Series(["-103.33213584 USDT", "1209.98 USDT", "[3.43893584 USDT]"])
    out = money_to_float(s)
    assert out.iloc[0] == pytest.approx(-103.33213584)
    assert out.iloc[1] == pytest.approx(1209.98)
    assert out.iloc[2] == pytest.approx(3.43893584)
    assert out.notna().all()


def test_money_to_float_survives_garbage():
    assert money_to_float(pd.Series(["", "None", "USDT"])).isna().all()


# -------------------------------- basics ------------------------------------ #
def test_empty_positions_gives_zeroed_metrics():
    m = compute(pd.DataFrame(), _equity([1000.0]), 1000.0, 0)
    assert m.trades == 0 and m.net_pnl == 0 and m.sharpe == 0


def test_trade_stats_are_exact():
    m = compute(_positions([100.0, -50.0, 200.0, -50.0]), _equity([1000.0, 1200.0]), 1000.0, 100)
    assert m.trades == 4
    assert m.net_pnl == pytest.approx(200.0)
    assert m.return_pct == pytest.approx(20.0)
    assert m.win_rate == pytest.approx(0.5)
    assert m.profit_factor == pytest.approx(300.0 / 100.0)
    assert m.avg_win == pytest.approx(150.0)
    assert m.avg_loss == pytest.approx(-50.0)
    assert m.payoff_ratio == pytest.approx(3.0)
    assert m.largest_win == 200.0
    assert m.largest_loss == -50.0
    assert m.fees_paid == pytest.approx(4.0)


def test_profit_factor_infinite_when_no_losses():
    m = compute(_positions([10.0, 20.0]), _equity([1000.0, 1030.0]), 1000.0, 10)
    assert m.profit_factor == float("inf")


# ------------------------------- drawdown ----------------------------------- #
def test_max_drawdown_is_peak_to_trough():
    m = compute(_positions([1.0]), _equity([100.0, 150.0, 75.0, 120.0]), 100.0, 4)
    assert m.max_drawdown_pct == pytest.approx(50.0)  # 150 -> 75


def test_monotonic_equity_has_no_drawdown():
    m = compute(_positions([1.0]), _equity([100.0, 110.0, 120.0]), 100.0, 3)
    assert m.max_drawdown_pct == pytest.approx(0.0)


def test_drawdown_needs_the_equity_curve_not_the_account_report():
    """F015: an equity curve without unrealised PnL cannot see an open-position
    drawdown at all."""
    flat = compute(_positions([1.0]), _equity([100.0, 100.0, 100.0]), 100.0, 3)
    marked = compute(_positions([1.0]), _equity([100.0, 60.0, 100.0]), 100.0, 3)
    assert flat.max_drawdown_pct == 0.0
    assert marked.max_drawdown_pct == pytest.approx(40.0)


# --------------------------------- sharpe ----------------------------------- #
def test_sharpe_matches_hand_calculation():
    rng = np.random.default_rng(7)
    rets = rng.normal(0.0002, 0.01, 2000)
    equity = _equity(list(1000.0 * np.cumprod(1 + rets)))
    m = compute(_positions([1.0]), equity, 1000.0, len(rets), timeframe="1h")

    series = equity.pct_change().dropna()
    expected = series.mean() / series.std() * math.sqrt(PERIODS_PER_YEAR["1h"])
    assert m.sharpe == pytest.approx(expected, rel=1e-9)


def test_sharpe_sign_follows_returns():
    up = compute(_positions([1.0]), _equity([100 * 1.001**i for i in range(500)]), 100.0, 500)
    down = compute(_positions([-1.0]), _equity([100 * 0.999**i for i in range(500)]), 100.0, 500)
    assert up.sharpe > 0
    assert down.sharpe < 0, "a losing equity curve must not report ~zero Sharpe"


def test_sharpe_scales_with_timeframe():
    """Annualisation must use the bar period: 4h bars are 6/day, not 24."""
    eq = _equity([100 * 1.0005**i for i in range(400)], freq="4h")
    h1 = compute(_positions([1.0]), eq, 100.0, 400, timeframe="1h")
    h4 = compute(_positions([1.0]), eq, 100.0, 400, timeframe="4h")
    assert h1.sharpe > h4.sharpe
    assert h1.sharpe / h4.sharpe == pytest.approx(
        math.sqrt(PERIODS_PER_YEAR["1h"] / PERIODS_PER_YEAR["4h"]), rel=1e-6
    )


def test_zero_volatility_gives_zero_sharpe_not_nan():
    m = compute(_positions([1.0]), _equity([100.0] * 50), 100.0, 50)
    assert m.sharpe == 0.0
    assert not math.isnan(m.sharpe)


def test_sortino_penalises_only_downside():
    """Two curves with the same mean return: the one whose losses are gentler
    must score better on Sortino."""
    a = _equity([100, 90, 110, 100, 120, 110, 130])
    b = _equity([100, 99, 104, 103, 112, 111, 130])
    ma = compute(_positions([1.0]), a, 100.0, 7)
    mb = compute(_positions([1.0]), b, 100.0, 7)
    assert mb.sortino > ma.sortino


# ------------------------------- red flags ---------------------------------- #
def test_red_flag_on_implausible_sharpe():
    rng = np.random.default_rng(3)
    eq = _equity(list(1000.0 * np.cumprod(1 + rng.normal(0.002, 0.002, 1500))))
    m = compute(_positions([1.0]), eq, 1000.0, 1500)
    assert m.sharpe > 2.0
    assert any("Sharpe" in f for f in m.red_flags())


def test_red_flag_on_zero_fees():
    m = compute(_positions([10.0, -5.0], fees=0.0), _equity([100.0, 105.0]), 100.0, 2)
    assert any("fees" in f for f in m.red_flags())


def test_red_flag_on_high_win_rate():
    m = compute(_positions([10.0] * 9 + [-5.0]), _equity([100.0, 190.0]), 100.0, 10)
    assert m.win_rate > 0.6
    assert any("win rate" in f for f in m.red_flags())


def _equity_with_exact_sharpe(
    target_sharpe: float, n: int = 3000, timeframe: str = "1h"
) -> pd.Series:
    """Equity curve whose realised annualised Sharpe is exactly `target_sharpe`.

    Returns are standardised to a controlled mean and sd rather than trusting a
    random draw: with a few thousand samples, sampling error on the mean is
    large enough to turn an intended 0.78 Sharpe into 2.61, which made an
    earlier version of this test fail for reasons unrelated to the code.
    """
    rng = np.random.default_rng(11)
    raw = rng.normal(0.0, 1.0, n)
    raw = (raw - raw.mean()) / raw.std()  # exact mean 0, sd 1
    sd = 0.006
    mean = target_sharpe / math.sqrt(PERIODS_PER_YEAR[timeframe]) * sd
    rets = raw * sd + mean
    return _equity(list(1000.0 * np.cumprod(1 + rets)), freq=timeframe)


def test_no_red_flags_on_realistic_trend_following():
    """A Sharpe-0.7, 38%-win-rate, payoff-2.0 profile is exactly what S001
    should look like if it works -- and must raise no red flags."""
    eq = _equity_with_exact_sharpe(0.7)
    pnls = [40.0] * 38 + [-20.0] * 62
    m = compute(_positions(pnls), eq, 1000.0, 3000)
    assert m.sharpe == pytest.approx(0.7, rel=0.02)
    assert m.win_rate == pytest.approx(0.38)
    assert m.payoff_ratio == pytest.approx(2.0)
    assert m.red_flags() == []


def test_sharpe_target_helper_is_accurate():
    for target in (0.4, 1.0, 1.9):
        m = compute(_positions([1.0]), _equity_with_exact_sharpe(target), 1000.0, 3000)
        assert m.sharpe == pytest.approx(target, rel=0.02)


# ------------------- return basis (F021 regression) ------------------------- #
def test_return_is_mark_to_market_not_realised_over_initial():
    """F021: `realized_pnl.sum() / starting_equity` sums dollars earned at
    different equity levels and divides by the initial one.

    Under compounding that is meaningless: one slice reported -10% while its
    equity curve rose 69%, because the realised figure ignored both compounding
    and open positions.
    """
    # Equity doubles; realised PnL is small because winners are still open.
    m = compute(_positions([50.0, -20.0]), _equity([1000.0, 1500.0, 2000.0]), 1000.0, 3)
    assert m.return_pct == pytest.approx(100.0), "mark-to-market return"
    assert m.realized_return_pct == pytest.approx(3.0), "realised over initial"


def test_unrealised_share_is_reported():
    """Profit sitting in open positions is not locked in and depends on the
    cut-off date, so its share of final equity is surfaced."""
    m = compute(_positions([100.0]), _equity([1000.0, 2000.0]), 1000.0, 2)
    # final 2000, realised 1000+100 = 1100 -> 900 unrealised = 45%
    assert m.unrealized_at_end_pct == pytest.approx(45.0, abs=0.5)


def test_red_flag_on_large_unrealised_share():
    m = compute(_positions([10.0]), _equity([1000.0, 5000.0]), 1000.0, 2)
    assert any("UNREALISED" in f for f in m.red_flags())


def test_no_unrealised_flag_when_flat_at_end():
    m = compute(_positions([500.0]), _equity([1000.0, 1500.0]), 1000.0, 2)
    assert m.unrealized_at_end_pct == pytest.approx(0.0)
    assert not any("UNREALISED" in f for f in m.red_flags())


def test_losing_equity_curve_cannot_show_positive_return():
    m = compute(_positions([100.0, 100.0]), _equity([1000.0, 900.0, 800.0]), 1000.0, 3)
    assert m.return_pct < 0, "mark-to-market return must follow the equity curve"
    assert m.realized_return_pct > 0, "realised PnL can disagree, and is reported separately"
