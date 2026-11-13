"""S008 mean-reversion signal tests.

Three properties are load-bearing and each has a test that fails loudly if it
breaks:

1. **The window is prior-only.** If the current close is inside the window it is
   measured against, it drags the mean towards itself and inflates the sigma it
   is divided by -- a close at exactly -2 sigma of the prior window measures
   -1.41 sigma. The rule would then quietly stop firing at its own threshold.
2. **It does not fire when it should not.** A dislocation just inside the
   threshold, and a quiet market whose sigma is measurement noise, must both be
   silent -- and the quiet-market veto must be counted (R40).
3. **Only closed bars, and only the close.** The rule may not read the current
   bar's high or low as though it were tradable intrabar (R8/F014).
"""

from __future__ import annotations

import math

import pandas as pd
import pytest
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from ntx.strategies.mean_reversion import MeanReversionConfig, MeanReversionStrategy
from ntx.strategies.signal import Direction

INST = TestInstrumentProvider.ethusdt_binance()
BT = BarType.from_str(f"{INST.id}-1-HOUR-LAST-EXTERNAL")
H = 3_600_000_000_000
T0 = int(pd.Timestamp("2024-01-01", tz="UTC").value)


def _bar(i: int, close: float, high: float | None = None, low: float | None = None) -> Bar:
    ts = T0 + i * H
    return Bar(
        bar_type=BT,
        open=INST.make_price(close),
        high=INST.make_price(high if high is not None else close + 1.0),
        low=INST.make_price(low if low is not None else close - 1.0),
        close=INST.make_price(close),
        volume=INST.make_qty(1),
        ts_event=ts,
        ts_init=ts,
    )


def _strategy(**kw) -> MeanReversionStrategy:
    cfg = MeanReversionConfig(instrument_ids=[str(INST.id)], **kw)
    s = MeanReversionStrategy(cfg)
    s.on_indicators_init(INST.id)
    return s


def _feed(s: MeanReversionStrategy, bar: Bar):
    """The real per-bar pipeline: `_ingest` records the bar via
    `on_indicators_update` (history included, R46/F033) and only then asks for a
    signal. Tests must never call `on_bar_signal` without the update first, or
    they would test an order of operations the framework does not use."""
    s.on_indicators_update(bar)
    return s.on_bar_signal(bar)


# ------------------------------- guards ------------------------------------- #
def test_lookback_minimum():
    with pytest.raises(ValueError, match="lookback must be >= 2"):
        _strategy(lookback=1)


def test_entry_z_must_be_positive():
    with pytest.raises(ValueError, match="entry_z must be > 0"):
        _strategy(entry_z=0.0)


def test_sigma_floor_must_not_be_negative():
    with pytest.raises(ValueError, match="min_sigma_price_pct must be >= 0"):
        _strategy(min_sigma_price_pct=-0.1)


def test_warmup_covers_window_and_atr():
    # one extra bar because the window is the `lookback` bars BEFORE the current
    assert _strategy(lookback=96, atr_period=14).warmup_bars() == 97
    assert _strategy(lookback=20, atr_period=56).warmup_bars() == 57


# ----------------- the window must exclude the current bar ------------------ #
# Closes 98..102: mean 100, population sigma sqrt(2). A close two sigma below
# that mean is 100 - 2*sqrt(2) = 97.1716. If the current bar were inside its own
# window the statistics would be mean 99.528 / sigma 1.667 and the same close
# would measure only -1.41 sigma -- silently below a threshold of 2.0.
_PRIOR = [98.0, 99.0, 100.0, 101.0, 102.0]
_SIGMA = math.sqrt(2.0)


def _warm(s: MeanReversionStrategy) -> None:
    for i, c in enumerate(_PRIOR):
        assert _feed(s, _bar(i, c)) is None, "no signal until the window is full"


def test_window_excludes_the_current_bar():
    s = _strategy(lookback=5, entry_z=2.0, min_sigma_price_pct=0.0)
    _warm(s)
    _feed(s, _bar(5, 97.17))
    st = s.stats(INST.id)
    assert st is not None
    mean, sigma = st
    assert mean == pytest.approx(100.0)
    assert sigma == pytest.approx(_SIGMA)
    # The contaminated values, spelled out so a regression is unmistakable.
    assert mean != pytest.approx(99.528, abs=1e-3)
    assert sigma != pytest.approx(1.667, abs=1e-3)


def test_two_sigma_below_the_prior_mean_is_a_long():
    s = _strategy(lookback=5, entry_z=2.0, min_sigma_price_pct=0.0)
    _warm(s)
    sig = _feed(s, _bar(5, 100.0 - 2.0 * _SIGMA))
    assert sig is not None and sig.direction is Direction.LONG
    assert s.zscore(INST.id, 97.17) == pytest.approx(-2.0, abs=0.01)


def test_two_sigma_above_the_prior_mean_is_a_short():
    s = _strategy(lookback=5, entry_z=2.0, min_sigma_price_pct=0.0)
    _warm(s)
    sig = _feed(s, _bar(5, 100.0 + 2.0 * _SIGMA))
    assert sig is not None and sig.direction is Direction.SHORT


def test_just_inside_the_threshold_does_not_fire():
    """The discriminating negative case: 1.9 sigma at a 2.0 threshold."""
    s = _strategy(lookback=5, entry_z=2.0, min_sigma_price_pct=0.0)
    _warm(s)
    assert _feed(s, _bar(5, 100.0 - 1.9 * _SIGMA)) is None


def test_a_close_at_the_mean_does_not_fire():
    s = _strategy(lookback=5, entry_z=2.0, min_sigma_price_pct=0.0)
    _warm(s)
    assert _feed(s, _bar(5, 100.0)) is None


def test_no_signal_before_the_window_is_full():
    s = _strategy(lookback=10, entry_z=0.5, min_sigma_price_pct=0.0)
    for i in range(10):
        # a violent, obviously-dislocated series: still silent, the window is short
        assert _feed(s, _bar(i, 100.0 + 10.0 * (-1) ** i)) is None
    assert s.signal_ready(INST.id) is False
    _feed(s, _bar(10, 100.0))
    assert s.signal_ready(INST.id) is True


def test_signal_ready_is_about_the_window_not_a_bar_count():
    """R52/R-01: 'a signal is possible'. A second instrument that has seen no
    bars is not ready just because the first one has."""
    other = TestInstrumentProvider.btcusdt_binance().id
    s = _strategy(lookback=5)
    _warm(s)
    _feed(s, _bar(5, 100.0))
    assert s.signal_ready(INST.id) is True
    assert s.signal_ready(other) is False


# ---------------------------- the sigma floor ------------------------------- #
def test_quiet_market_is_vetoed_and_counted():
    """Five near-identical closes then a 0.05 move is not a dislocation; the raw
    z-score of ~11 is an artefact of the divisor. R40: the veto is counted."""
    s = _strategy(lookback=5, entry_z=2.0, min_sigma_price_pct=0.05)
    quiet = [100.00, 100.01, 100.00, 100.01, 100.00]
    for i, c in enumerate(quiet):
        _feed(s, _bar(i, c))
    assert s.filtered_out == 0
    assert _feed(s, _bar(5, 99.95)) is None
    assert s.filtered_out == 1, "the suppressed signal must be visible in counters"


def test_the_floor_only_counts_signals_it_actually_suppressed():
    """A quiet market with no dislocation must not inflate the veto count --
    otherwise the counter measures 'the branch was reached', not 'a trade was
    prevented', and stops distinguishing a live guard from a dead one."""
    s = _strategy(lookback=5, entry_z=2.0, min_sigma_price_pct=0.05)
    for i, c in enumerate([100.00, 100.01, 100.00, 100.01, 100.00]):
        _feed(s, _bar(i, c))
    assert _feed(s, _bar(5, 100.005)) is None
    assert s.filtered_out == 0


def test_the_floor_can_be_turned_off():
    """R40 in the other direction: prove the knob is the thing doing the work."""
    s = _strategy(lookback=5, entry_z=2.0, min_sigma_price_pct=0.0)
    for i, c in enumerate([100.00, 100.01, 100.00, 100.01, 100.00]):
        _feed(s, _bar(i, c))
    sig = _feed(s, _bar(5, 99.95))
    assert sig is not None and sig.direction is Direction.LONG
    assert s.filtered_out == 0


def test_zero_sigma_does_not_divide_by_zero():
    s = _strategy(lookback=5, entry_z=2.0, min_sigma_price_pct=0.0)
    for i in range(5):
        _feed(s, _bar(i, 100.0))
    assert _feed(s, _bar(5, 90.0)) is None
    assert s.zscore(INST.id, 90.0) is None
    assert s.filtered_out == 0


# --------------------------- lookahead (R8 / F014) -------------------------- #
def _series(n: int) -> list[float]:
    """Deterministic, dislocation-rich, no randomness in the test."""
    return [100.0 + 8.0 * math.sin(i / 3.0) + 3.0 * math.sin(i / 1.7) for i in range(n)]


def test_a_bars_signal_does_not_depend_on_later_bars():
    """Causality. Feed the whole series and record every decision, then replay
    each prefix into a fresh strategy: the decision at bar i must be identical.
    A rule that indexed a stored history wrongly -- the classic way F014 gets
    reintroduced during a refactor -- would diverge here."""
    closes = _series(40)
    full = _strategy(lookback=10, entry_z=1.0, min_sigma_price_pct=0.0)
    decided = [_feed(full, _bar(i, c)) for i, c in enumerate(closes)]

    for i in (11, 17, 23, 31, 39):
        prefix = _strategy(lookback=10, entry_z=1.0, min_sigma_price_pct=0.0)
        got = None
        for j, c in enumerate(closes[: i + 1]):
            got = _feed(prefix, _bar(j, c))
        want = decided[i]
        assert (got is None) == (want is None), f"bar {i}: presence of a signal changed"
        if want is not None:
            assert got is not None and got.direction is want.direction, f"bar {i}: direction"


def test_the_rule_reads_the_close_only_not_the_bars_high_or_low():
    """R8: the strategy sees a completed bar and transacts at its close. A high
    or low it could never have traded at must not enter the decision."""
    closes = _series(20)
    tight = _strategy(lookback=10, entry_z=1.0, min_sigma_price_pct=0.0)
    wild = _strategy(lookback=10, entry_z=1.0, min_sigma_price_pct=0.0)
    for i, c in enumerate(closes):
        a = _feed(tight, _bar(i, c, high=c + 0.01, low=c - 0.01))
        b = _feed(wild, _bar(i, c, high=c + 60.0, low=c - 60.0))
        assert (a is None) == (b is None), f"bar {i}: high/low changed whether it fired"
        if a is not None and b is not None:
            assert a.direction is b.direction, f"bar {i}: high/low changed the direction"


# ------------------------------- hygiene ------------------------------------ #
def test_history_is_bounded():
    """A four-year, ten-instrument backtest must not accumulate a list per bar."""
    s = _strategy(lookback=96)
    for i in range(3_000):
        _feed(s, _bar(i, 100.0 + (i % 7)))
    assert len(s._closes[INST.id]) <= 98


def test_inherits_framework_defaults_unchanged():
    """R24: a new strategy is a signal, not a system. It must pick up the risk
    layer and the exit stack rather than redefining them."""
    s = _strategy()
    assert s.limits.risk_equity_pct == 0.5
    assert s.limits.max_leverage == 3.0
    assert s.exit_cfg.sl_atr_mult == 2.0  # the ATR stop is what caps the left tail
    assert s.exit_cfg.tsl_enabled is True
    assert s.exit_cfg.target_roi_pct is None  # ADR-0008
