"""S007 fast Donchian tests.

Two things are load-bearing here and neither is tested by `test_donchian.py`:

1. **The exit scaling.** S007 exists to run S003's rule on a shorter bar. If the
   stop and trailing distances are not widened to match, the backtest measures
   the exit stack rather than the breakout rule -- a 1.0% trailing stop
   evaluated 16x more often is hit by noise, and a stop half as wide doubles
   notional under risk-based sizing and so doubles fee drag at an unchanged
   trade count.
2. **That the signal is identical to S003's.** S007 is a control. If it drifts
   from S003 by so much as a tie-break, the comparison the study reports is
   meaningless.

The lookahead property (R8/F014) is re-asserted against this class rather than
trusted by inheritance, because this is the class that would be deployed.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.donchian_fast import (
    REFERENCE_EXITS,
    REFERENCE_TIMEFRAME,
    TF_MINUTES,
    FastDonchianConfig,
    FastDonchianStrategy,
    scaled_exits,
)
from ntx.strategies.signal import Direction

INST = TestInstrumentProvider.ethusdt_binance()
BT = BarType.from_str(f"{INST.id}-15-MINUTE-LAST-EXTERNAL")
Q = 900_000_000_000  # 15 minutes in nanoseconds
T0 = int(pd.Timestamp("2024-01-01", tz="UTC").value)


def _bar(i: int, high: float, low: float, close: float) -> Bar:
    # nautilus rejects a bar whose extremes do not bracket open/close, so the
    # extremes are widened to contain the close rather than the close being
    # moved -- the close is what every assertion below is about.
    high, low = max(high, close), min(low, close)
    ts = T0 + i * Q
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


def _walk(n: int) -> list[Bar]:
    """A deterministic pseudo-random price walk. A cyclic pattern will not do:
    its closes never escape the range of the prior channel, so it produces zero
    signals and any comparison over it is vacuous."""
    bars, price, seed = [], 100.0, 12345
    for i in range(n):
        seed = (1103515245 * seed + 12345) % 2147483648
        step = ((seed >> 8) % 1000) / 1000.0 - 0.5  # [-0.5, 0.5)
        price = max(5.0, price + step * 4.0)
        wick = 0.5 + ((seed >> 20) % 100) / 100.0
        bars.append(_bar(i, price + wick, price - wick, price))
    return bars


def _strategy(**kw) -> FastDonchianStrategy:
    cfg = FastDonchianConfig(instrument_ids=[str(INST.id)], **kw)
    s = FastDonchianStrategy(cfg)
    s.on_indicators_init(INST.id)
    return s


def _feed(s, bar):
    """The real per-bar pipeline: `_ingest` records the bar via
    `on_indicators_update` (history included) and only then asks for a signal."""
    s.on_indicators_update(bar)
    return s.on_bar_signal(bar)


# ----------------------------- exit scaling --------------------------------- #
def test_scaling_is_identity_at_the_reference_bar():
    assert scaled_exits(REFERENCE_TIMEFRAME) == REFERENCE_EXITS


def test_faster_bars_get_a_wider_stop_and_a_wider_trail():
    """Every distance must grow monotonically as the bar shrinks. A regression
    that left any of them at the 4h value would silently re-introduce the
    confound this module exists to remove."""
    order = sorted(TF_MINUTES, key=lambda tf: TF_MINUTES[tf])  # 15m, 1h, 4h
    for key in REFERENCE_EXITS:
        values = [scaled_exits(tf)[key] for tf in order]
        assert values == sorted(values, reverse=True), f"{key} not monotonic: {values}"


def test_stop_width_invariant_holds_across_timeframes():
    """The actual invariant, not just 'bigger'. A bar's true range grows with
    sqrt(duration), so `sl_atr_mult * sqrt(bar_minutes)` -- the stop as a
    fraction of price -- must be the same number at every timeframe."""
    widths = {tf: scaled_exits(tf)["sl_atr_mult"] * math.sqrt(TF_MINUTES[tf]) for tf in TF_MINUTES}
    reference = widths[REFERENCE_TIMEFRAME]
    for tf, w in widths.items():
        assert w == pytest.approx(reference, rel=1e-9), f"{tf} stop width {w} != {reference}"


def test_trail_stays_the_same_multiple_of_the_stop():
    for tf in TF_MINUTES:
        e = scaled_exits(tf)
        assert e["tsl_trail_price_pct"] / e["sl_atr_mult"] == pytest.approx(
            REFERENCE_EXITS["tsl_trail_price_pct"] / REFERENCE_EXITS["sl_atr_mult"]
        )


def test_unknown_timeframe_is_refused_not_guessed():
    """A silent default here would scale a 5m run as if it were 4h."""
    with pytest.raises(ValueError, match="unknown timeframe"):
        scaled_exits("5m")


def test_atr_period_is_not_part_of_the_scaling():
    """Lengthening the ATR smooths it, it does not widen it -- so scaling
    `atr_period` would not restore stop width, it would only inflate
    `warmup_bars()` and therefore the history request made on every restart."""
    assert "atr_period" not in scaled_exits("15m")


def test_warmup_stays_within_the_live_history_request():
    """Live warm-up asks for `warmup_bars() + 5` bars in ONE call and no one has
    measured OKX's per-request candle cap, so the periods this study actually
    sweeps must stay well inside it."""
    for entry in (15, 20, 30, 55):
        s = _strategy(entry_period=entry, exit_period=max(1, entry // 2), atr_period=14)
        assert s.warmup_bars() <= 100


# ------------------------------ the signal ---------------------------------- #
def test_breakout_above_the_prior_channel_goes_long():
    s = _strategy(entry_period=5, exit_period=2)
    for i in range(5):
        assert _feed(s, _bar(i, 101.0, 99.0, 100.0)) is None
    sig = _feed(s, _bar(5, 120.0, 99.0, 115.0))
    assert sig is not None and sig.direction is Direction.LONG


def test_breakdown_below_the_prior_channel_goes_short():
    s = _strategy(entry_period=5, exit_period=2)
    for i in range(5):
        _feed(s, _bar(i, 101.0, 99.0, 100.0))
    sig = _feed(s, _bar(5, 101.0, 80.0, 85.0))
    assert sig is not None and sig.direction is Direction.SHORT


def test_a_close_inside_the_channel_does_not_fire():
    s = _strategy(entry_period=5, exit_period=2)
    for i in range(6):
        _feed(s, _bar(i, 110.0, 90.0, 100.0))
    assert _feed(s, _bar(6, 109.9, 90.1, 109.0)) is None


def test_touching_the_channel_edge_exactly_does_not_fire():
    """`>` not `>=`. At a shorter bar the channel is touched constantly, and an
    off-by-one here would add a large number of zero-edge, fee-only trades."""
    s = _strategy(entry_period=5, exit_period=2)
    for i in range(6):
        _feed(s, _bar(i, 110.0, 90.0, 100.0))
    assert _feed(s, _bar(6, 110.0, 90.0, 110.0)) is None


def test_no_signal_while_the_channel_is_still_filling():
    s = _strategy(entry_period=10, exit_period=3)
    for i in range(10):
        assert _feed(s, _bar(i, 100.0 + i, 90.0 - i, 95.0)) is None, f"fired at bar {i}"


def test_rolling_buffers_stay_bounded():
    s = _strategy(entry_period=20, exit_period=10)
    for i in range(2_000):
        _feed(s, _bar(i, 101.0, 99.0, 100.0))
    assert len(s._highs[INST.id]) <= 22
    assert len(s._lows[INST.id]) <= 22


# --------------------------- lookahead (R8 / F014) -------------------------- #
def test_an_intrabar_spike_that_closes_inside_is_not_a_breakout():
    """The strategy transacts at a bar's CLOSE. A high that pierced the channel
    during the bar was never tradeable, so it must not produce a signal --
    reading `bar.high` as if it were actionable is exactly F014."""
    s = _strategy(entry_period=5, exit_period=2)
    for i in range(6):
        _feed(s, _bar(i, 110.0, 90.0, 100.0))
    assert _feed(s, _bar(6, 500.0, 90.0, 100.0)) is None, "intrabar high is not tradeable"
    assert _feed(s, _bar(7, 100.0, 0.5, 100.0)) is None, "intrabar low is not tradeable"


def test_a_bar_enters_the_channel_only_after_it_has_closed():
    """The complement of the test above, and the reason the channel excludes the
    bar being judged. Once the spike bar has closed it raises the channel, so a
    later close above the OLD high is no longer a breakout."""
    s = _strategy(entry_period=5, exit_period=2)
    for i in range(6):
        _feed(s, _bar(i, 110.0, 90.0, 100.0))
    assert s.channel(INST.id, 5) == (110.0, 90.0)
    _feed(s, _bar(6, 500.0, 90.0, 100.0))  # spike bar, closes back inside
    # A close of 200 would have broken the OLD 110 channel comfortably. It does
    # not fire, because by the time bar 7 is judged the closed spike bar has
    # raised the channel to 500 -- the channel is built from history, never from
    # the bar being judged.
    assert _feed(s, _bar(7, 200.0, 90.0, 200.0)) is None, "below the raised channel high"
    assert s.channel(INST.id, 5)[0] == 500.0


def test_the_decision_at_a_bar_is_unchanged_by_what_comes_after_it():
    """No state used by the signal may be written by a later bar. Replaying a
    prefix and then the full series must give the same verdict at the shared
    bar."""
    bars = _walk(60)
    a = _strategy(entry_period=10, exit_period=4)
    first = [_feed(a, b) for b in bars[:25]]
    b_ = _strategy(entry_period=10, exit_period=4)
    second = [_feed(b_, b) for b in bars]
    for i, (x, y) in enumerate(zip(first, second[:25], strict=True)):
        assert (x is None) == (y is None), f"bar {i} differs"
        if x is not None:
            assert x.direction is y.direction and x.reason == y.reason


# ------------------------- S007 must remain the control --------------------- #
def test_the_signal_is_identical_to_s003_bar_for_bar():
    """S007 is a control for S003. It is allowed to differ in configuration and
    in nothing else; an override of `on_bar_signal` would invalidate every
    comparison the study reports."""
    kw = dict(entry_period=12, exit_period=5)
    fast = _strategy(**kw)
    base_cfg = DonchianBreakoutConfig(instrument_ids=[str(INST.id)], **kw)
    base = DonchianBreakoutStrategy(base_cfg)
    base.on_indicators_init(INST.id)
    fired = 0
    for i, bar in enumerate(_walk(400)):
        a, b = _feed(fast, bar), _feed(base, bar)
        assert (a is None) == (b is None), f"bar {i}: {a} vs {b}"
        if a is not None:
            assert a.direction is b.direction and a.reason == b.reason
            fired += 1
    assert fired > 0, "the sequence produced no signals -- the test proves nothing"


def test_config_is_a_distinct_type_so_the_registry_can_pin_it_separately():
    """One node per strategy, one pinned config per strategy (R47/F043). A
    shared config class would let a change to S003's defaults move S007's live
    parameters."""
    assert FastDonchianConfig is not DonchianBreakoutConfig
    assert issubclass(FastDonchianConfig, DonchianBreakoutConfig)
    assert issubclass(FastDonchianStrategy, DonchianBreakoutStrategy)


def test_inherits_the_risk_layer_and_exit_stack_unchanged():
    """R24: a new strategy is a signal, not a system."""
    s = _strategy(**scaled_exits("15m"))
    assert s.limits.max_leverage == 3.0
    assert s.exit_cfg.sl_atr_mult == 8.0
    assert s.exit_cfg.target_roi_pct is None  # ADR-0008
    assert s.exit_cfg.tsl_enabled is True
