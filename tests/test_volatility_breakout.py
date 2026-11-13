"""Volatility squeeze-breakout signal tests.

Two properties carry the whole idea and are tested hardest:

1. **Nothing may read the bar it is judging.** The breakout range excludes it
   (or `close > high` is tautological) and so does the compression reading. The
   second is the subtle one: a breakout bar is by construction a wide bar, so
   including it in the band-width ranking would make the squeeze filter reject
   its own signals -- silently, while still producing an equity curve.
2. **Each gate must actually block.** A filter that never rejects anything and a
   filter that is never reached look identical in a results table (R40), so the
   squeeze gate and the expansion gate are each shown blocking a bar that the
   other two gates pass, and then passing it when the knob is turned off.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from ntx.strategies.signal import Direction
from ntx.strategies.volatility_breakout import (
    VolatilityBreakoutConfig,
    VolatilityBreakoutStrategy,
)

INST = TestInstrumentProvider.ethusdt_binance()
BT = BarType.from_str(f"{INST.id}-1-HOUR-LAST-EXTERNAL")
H = 3_600_000_000_000
T0 = int(pd.Timestamp("2024-01-01", tz="UTC").value)

# Small on purpose: warm-up is max(range_period, bb_period + squeeze_lookback - 1,
# atr_period) + 1 = max(5, 9, 2) + 1 = 10 bars, so bar index 9 is the first one
# that can produce a signal and every fixture below is exactly 9 bars + 1.
SMALL = dict(range_period=5, bb_period=4, squeeze_lookback=6, atr_period=2)

# closes of a noisy stretch that then goes quiet: band width at bar 8 is the
# lowest of the lookback, so bar 9 breaks out OF a squeeze.
SQUEEZE_CLOSES = [100.0, 112.0, 98.0, 113.0, 99.0, 105.0, 105.0, 105.0, 105.0]
# The mirror image: quiet first, loud last. Bar 8's band width is the HIGHEST of
# the lookback, so bar 9 breaks out of noise, not out of a squeeze.
NOISY_CLOSES = [105.0, 105.0, 105.0, 105.0, 105.0, 100.0, 112.0, 98.0, 113.0]


def _bar(i: int, high: float, low: float, close: float) -> Bar:
    ts = T0 + i * H
    return Bar(
        bar_type=BT,
        open=INST.make_price(close),
        high=INST.make_price(high),
        low=INST.make_price(low),
        close=INST.make_price(close),
        volume=INST.make_qty(1),
        ts_event=ts,
        ts_init=ts,
    )


def _strategy(**kw) -> VolatilityBreakoutStrategy:
    cfg = VolatilityBreakoutConfig(instrument_ids=[str(INST.id)], **{**SMALL, **kw})
    return VolatilityBreakoutStrategy(cfg)


def _feed(s: VolatilityBreakoutStrategy, bar: Bar):
    """The real per-bar pipeline: `_ingest` records the bar via
    `on_indicators_update` (history included, R46/F033) and only then asks for a
    signal."""
    s.on_indicators_update(bar)
    return s.on_bar_signal(bar)


def _prefix(s: VolatilityBreakoutStrategy, closes: list[float]) -> None:
    """Feed a 9-bar prefix with a +-1 bar range around each close."""
    for i, c in enumerate(closes):
        assert _feed(s, _bar(i, c + 1.0, c - 1.0, c)) is None, f"bar {i} fired during warm-up"


# ------------------------------- guards ------------------------------------- #
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (dict(range_period=1), "range_period must be >= 2"),
        (dict(bb_period=1), "bb_period must be >= 2"),
        (dict(squeeze_lookback=1), "squeeze_lookback must be >= 2"),
        (dict(squeeze_percentile=0.0), "squeeze_percentile must be in"),
        (dict(squeeze_percentile=101.0), "squeeze_percentile must be in"),
        (dict(bb_std_mult=0.0), "bb_std_mult must be > 0"),
        (dict(expansion_tr_mult=-0.1), "expansion_tr_mult must be >= 0"),
    ],
)
def test_config_is_validated(kwargs, match):
    with pytest.raises(ValueError, match=match):
        _strategy(**kwargs)


def test_warmup_covers_range_squeeze_and_atr():
    # the ranked band width is the binding term here: 4 + 6 - 1 = 9, + 1 = 10
    assert _strategy().warmup_bars() == 10
    # ... but the ATR can bind instead, and must
    assert _strategy(atr_period=200).warmup_bars() == 201
    # ... and so can the breakout range
    assert _strategy(range_period=400).warmup_bars() == 401


def test_inherits_framework_defaults_unchanged():
    """A new strategy is a signal, not a system (R24)."""
    s = _strategy()
    assert s.limits.risk_equity_pct == 0.5
    assert s.limits.max_leverage == 3.0
    assert s.exit_cfg.sl_atr_mult == 2.0
    assert s.exit_cfg.tsl_enabled is True
    assert s.exit_cfg.target_roi_pct is None  # ADR-0008


# ------------------------------ readiness ----------------------------------- #
def test_no_signal_while_the_lookback_is_short():
    s = _strategy(squeeze_percentile=100.0)
    for i in range(9):
        # every bar a new high, so the breakout gate would pass on all of them
        assert _feed(s, _bar(i, 100.0 + i, 90.0, 99.0 + i)) is None
    assert s.signal_ready(INST.id) is False


def test_band_width_is_nan_until_its_window_is_full():
    """NaN, not 0.0: a zero would rank as the tightest squeeze ever recorded and
    would open a run's first trades on no information at all."""
    cfg = VolatilityBreakoutConfig(instrument_ids=[str(INST.id)], **SMALL)
    assert math.isnan(VolatilityBreakoutStrategy._band_width([1.0, 2.0, 3.0], cfg))
    assert VolatilityBreakoutStrategy._band_width([1.0, 2.0, 3.0, 4.0], cfg) > 0.0


def test_readiness_arrives_exactly_at_warmup_and_ignores_the_squeeze_knob():
    """The control cell (`squeeze_percentile=100`) must judge exactly the same
    bars as the filtered cells, or the A/B comparison is worthless.

    It also pins `warmup_bars()`: after 9 bars the band-width window still
    contains NaNs and no signal is possible; the 10th bar is the first that can
    produce one, which is what `warmup_bars() == 10` claims.
    """
    a, b = _strategy(squeeze_percentile=10.0), _strategy(squeeze_percentile=100.0)
    for s in (a, b):
        _prefix(s, SQUEEZE_CLOSES)
        assert s.signal_ready(INST.id) is False, "9 bars is not enough"
        _feed(s, _bar(9, 105.5, 104.5, 105.0))  # 10th bar, inside the range
        assert s.signal_ready(INST.id) is True
        assert s.warmup_bars() == 10


# --------------------------- it fires when it should ------------------------ #
def test_fires_long_on_a_break_out_of_a_squeeze():
    s = _strategy(squeeze_percentile=25.0)
    _prefix(s, SQUEEZE_CLOSES)
    sig = _feed(s, _bar(9, 121.0, 119.0, 120.0))
    # bar 8 was the quietest of the six-bar lookback: rank 1/6 of the readings
    assert s.squeeze_rank(INST.id) == pytest.approx(100.0 / 6.0)
    assert sig is not None and sig.direction is Direction.LONG
    assert "squeeze_break" in sig.reason


def test_fires_short_on_a_break_down_out_of_a_squeeze():
    s = _strategy(squeeze_percentile=25.0)
    _prefix(s, SQUEEZE_CLOSES)
    sig = _feed(s, _bar(9, 91.0, 89.0, 90.0))  # below the 98.0 range low
    assert sig is not None and sig.direction is Direction.SHORT


# --------------------------- it must NOT fire ------------------------------- #
def test_a_close_inside_the_range_is_not_a_breakout():
    s = _strategy(squeeze_percentile=100.0, expansion_tr_mult=0.0)
    _prefix(s, SQUEEZE_CLOSES)
    assert _feed(s, _bar(9, 105.9, 98.5, 105.5)) is None


def test_touching_the_range_edge_is_not_a_breakout():
    """The range high of the fixture is exactly 106.0; a close AT it is not a
    break. Strictness matters: `>=` would fire on every flat retest."""
    s = _strategy(squeeze_percentile=100.0, expansion_tr_mult=0.0)
    _prefix(s, SQUEEZE_CLOSES)
    assert _feed(s, _bar(9, 106.5, 104.0, 106.0)) is None


def test_squeeze_gate_blocks_a_break_out_of_noise():
    """Same break, same expansion -- only the prior volatility state differs."""
    s = _strategy(squeeze_percentile=25.0)
    _prefix(s, NOISY_CLOSES)
    assert _feed(s, _bar(9, 131.0, 129.0, 130.0)) is None
    assert s.squeeze_rank(INST.id) == pytest.approx(100.0)  # loudest of the lookback


def test_turning_the_squeeze_gate_off_lets_that_same_bar_through():
    """R40: proof the knob was turned, not that it silently did nothing."""
    s = _strategy(squeeze_percentile=100.0)
    _prefix(s, NOISY_CLOSES)
    sig = _feed(s, _bar(9, 131.0, 129.0, 130.0))
    assert sig is not None and sig.direction is Direction.LONG


def test_expansion_gate_blocks_a_quiet_drift_out_of_the_squeeze():
    s = _strategy(squeeze_percentile=25.0, expansion_tr_mult=1.0)
    _prefix(s, SQUEEZE_CLOSES)
    # breaks the 106.0 range high, but its true range is well under the recent mean
    assert _feed(s, _bar(9, 106.6, 106.4, 106.5)) is None


def test_turning_the_expansion_gate_off_lets_that_same_bar_through():
    s = _strategy(squeeze_percentile=25.0, expansion_tr_mult=0.0)
    _prefix(s, SQUEEZE_CLOSES)
    sig = _feed(s, _bar(9, 106.6, 106.4, 106.5))
    assert sig is not None and sig.direction is Direction.LONG


# ------------------------- causality / lookahead (R8, F014) ----------------- #
def test_breakout_range_excludes_the_bar_being_judged():
    s = _strategy()
    _prefix(s, SQUEEZE_CLOSES)
    _feed(s, _bar(9, 500.0, 1.0, 120.0))  # a bar far outside the range
    # bars 4..8 -- the five that closed BEFORE bar 9, not widened by bar 9 itself
    assert s.breakout_range(INST.id) == (pytest.approx(106.0), pytest.approx(98.0))


def test_squeeze_reading_excludes_the_bar_being_judged():
    """The load-bearing causality test for this strategy.

    Bar 9 is a violent expansion bar, so its own band width is the largest in the
    series. If the ranking used it, the rank would be 100 and the squeeze filter
    would veto the very breakout it exists to catch.
    """
    s = _strategy(squeeze_percentile=25.0)
    _prefix(s, SQUEEZE_CLOSES)
    sig = _feed(s, _bar(9, 121.0, 119.0, 120.0))
    assert sig is not None, "a squeeze break was vetoed -- the ranking read its own bar"
    # still bar 8's reading, the quietest of the lookback
    assert s.squeeze_rank(INST.id) == pytest.approx(100.0 / 6.0)
    widths = s._widths[INST.id]
    ranked, own = widths[-7:-1], widths[-1]
    assert own > ranked[-1], "fixture is wrong: bar 9 must have widened the bands"
    # had the ranking used bar 9's own width, the rank would have vetoed the trade
    would_be = 100.0 * sum(1 for w in ranked if w <= own) / len(ranked)
    assert would_be > 25.0


def test_a_later_bar_cannot_change_an_earlier_decision():
    """Feeding more history after the fact must not alter what bar 9 decided --
    the rule is a pure function of bars that had already closed."""
    early = _strategy(squeeze_percentile=25.0)
    _prefix(early, SQUEEZE_CLOSES)
    first = _feed(early, _bar(9, 121.0, 119.0, 120.0))
    _feed(early, _bar(10, 200.0, 50.0, 60.0))  # a wild future bar

    replay = _strategy(squeeze_percentile=25.0)
    _prefix(replay, SQUEEZE_CLOSES)
    again = _feed(replay, _bar(9, 121.0, 119.0, 120.0))
    assert first is not None and again is not None
    assert (first.direction, first.reason) == (again.direction, again.reason)


# ------------------------------- hygiene ------------------------------------ #
def test_rolling_buffers_are_bounded():
    """A multi-year backtest feeds ~58,000 bars per instrument."""
    s = _strategy(range_period=20, bb_period=10, squeeze_lookback=50)
    for i in range(2_000):
        _feed(s, _bar(i, 101.0, 99.0, 100.0))
    iid = INST.id
    assert len(s._highs[iid]) <= 22
    assert len(s._lows[iid]) <= 22
    assert len(s._closes[iid]) <= 22
    assert len(s._trs[iid]) <= 22
    assert len(s._widths[iid]) <= 52


def test_true_range_uses_the_previous_close():
    """A gap between bars is real risk; high-low alone would understate it and
    make the expansion gate fire on gaps it should have absorbed."""
    s = _strategy()
    _feed(s, _bar(0, 101.0, 99.0, 100.0))
    _feed(s, _bar(1, 121.0, 119.0, 120.0))  # gapped up: high-low is 2, true range is 21
    assert s._trs[INST.id][-1] == pytest.approx(21.0)
