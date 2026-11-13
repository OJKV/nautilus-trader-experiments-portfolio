"""S004 cross-sectional momentum tests.

The load-bearing properties are: (a) the ranking uses only bars that have
CLOSED, so a decision at bar T cannot move when T+1 arrives (R8/F014); (b) a
signal fires only on the rebalance grid, and that grid is the same for every
instrument regardless of how many bars each has seen; (c) rolling state is built
in `on_indicators_update`, so it survives warm-up replay (R46/F033).
"""

from __future__ import annotations

import pandas as pd
import pytest
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from ntx.strategies.cross_sectional_momentum import (
    CrossSectionalMomentumConfig,
    CrossSectionalMomentumStrategy,
)
from ntx.strategies.signal import Direction

# Six distinct instruments: enough to exercise a real ranking, and enough for a
# `min_ranked` of 5 to be a meaningful gate rather than a formality.
INSTRUMENTS = [
    TestInstrumentProvider.btcusdt_binance(),
    TestInstrumentProvider.ethusdt_binance(),
    TestInstrumentProvider.adausdt_binance(),
    TestInstrumentProvider.btcusdt_perp_binance(),
    TestInstrumentProvider.ethusdt_perp_binance(),
    TestInstrumentProvider.xrpusdt_linear_bybit(),
]
BY_ID = {i.id: i for i in INSTRUMENTS}
BAR_TYPES = {i.id: BarType.from_str(f"{i.id}-1-HOUR-LAST-EXTERNAL") for i in INSTRUMENTS}

H = 3_600_000_000_000
# Chosen so that index 0 of the hourly grid falls on this timestamp: the
# rebalance grid is derived from absolute ts_event, so a test that wants bar i
# to be "on the grid" must start from a grid-aligned epoch.
T0 = int(pd.Timestamp("2024-01-01", tz="UTC").value)
assert T0 % H == 0


def _bar(inst, i: int, close: float) -> Bar:
    ts = T0 + i * H
    price = inst.make_price(close)
    return Bar(
        bar_type=BAR_TYPES[inst.id],
        open=price,
        high=price,
        low=price,
        close=price,
        volume=inst.make_qty(1),
        ts_event=ts,
        ts_init=ts,
    )


def _strategy(**kw) -> CrossSectionalMomentumStrategy:
    kw.setdefault("lookback", 4)
    kw.setdefault("rebalance_bars", 2)
    kw.setdefault("long_n", 2)
    kw.setdefault("min_ranked", 6)
    cfg = CrossSectionalMomentumConfig(instrument_ids=[str(i.id) for i in INSTRUMENTS], **kw)
    return CrossSectionalMomentumStrategy(cfg)


def _feed(s, bar):
    """The real per-bar pipeline: `_ingest` records the bar via
    `on_indicators_update` and only then asks for a signal."""
    s.on_indicators_update(bar)
    return s.on_bar_signal(bar)


def _feed_all(s, i: int, closes: dict) -> dict:
    """Feed bar `i` for every instrument, return each one's signal."""
    return {inst.id: _feed(s, _bar(inst, i, closes[inst.id])) for inst in INSTRUMENTS}


def _flat_prices(value: float = 100.0) -> dict:
    return dict.fromkeys(BY_ID, value)


# ------------------------------- guards ------------------------------------- #
def test_lookback_minimum():
    with pytest.raises(ValueError, match="lookback must be >= 2"):
        _strategy(lookback=1)


def test_rebalance_minimum():
    with pytest.raises(ValueError, match="rebalance_bars must be >= 1"):
        _strategy(rebalance_bars=0)


def test_something_must_be_held():
    with pytest.raises(ValueError, match="nothing is ever held"):
        _strategy(long_n=0, short_n=0)


def test_min_ranked_must_cover_both_sets():
    with pytest.raises(ValueError, match="min_ranked"):
        _strategy(long_n=3, short_n=3, min_ranked=5, max_concurrent_positions=6)


def test_shorts_refused_when_the_framework_would_drop_them():
    """R40: `allow_short=False` silently discards every SHORT in `base.on_bar`.
    A long/short config that quietly runs long-only is the knob-that-did-nothing
    failure, so it must not be constructible."""
    with pytest.raises(ValueError, match="allow_short"):
        _strategy(long_n=2, short_n=2, allow_short=False, max_concurrent_positions=4)


def test_asking_for_more_names_than_the_cap_holds_is_refused():
    """Respect the position cap rather than fight it: over the cap, which names
    get in is decided by bar arrival order, not by rank."""
    with pytest.raises(ValueError, match="max_concurrent_positions"):
        _strategy(long_n=3, short_n=3, min_ranked=6, max_concurrent_positions=3)


def test_warmup_covers_lookback_and_atr():
    assert _strategy(lookback=60, atr_period=14).warmup_bars() == 61
    assert _strategy(lookback=10, atr_period=100).warmup_bars() == 101


# ------------------------------ scoring ------------------------------------- #
def test_score_is_none_until_the_lookback_window_is_complete():
    s = _strategy(lookback=4)
    inst = INSTRUMENTS[0]
    for i in range(4):
        _feed(s, _bar(inst, i, 100.0 + i))
        assert s.score(inst.id) is None, f"scored after only {i + 1} bars"
    _feed(s, _bar(inst, 4, 110.0))
    assert s.score(inst.id) == pytest.approx(0.10)  # 110/100 - 1, over 4 bars


def test_score_is_a_return_not_a_price_level():
    """A cheap coin that doubled must outrank an expensive coin that crawled."""
    s = _strategy(lookback=4)
    cheap, dear = INSTRUMENTS[2], INSTRUMENTS[0]
    for i in range(5):
        _feed(s, _bar(cheap, i, 1.0 if i == 0 else 2.0))
        _feed(s, _bar(dear, i, 40000.0 if i == 0 else 40400.0))
    assert s.score(cheap.id) > s.score(dear.id)


def test_history_is_bounded():
    """Rolling buffers must not grow over a multi-year backtest."""
    s = _strategy(lookback=4)
    inst = INSTRUMENTS[0]
    for i in range(500):
        _feed(s, _bar(inst, i, 100.0 + (i % 7)))
    assert len(s._closes[inst.id]) <= 6


# ------------------------------ readiness ----------------------------------- #
def test_not_ready_until_enough_instruments_are_scored():
    """A ranking rule needs a field to rank: one warm instrument is not
    readiness (R52/R-01)."""
    s = _strategy(lookback=4, min_ranked=6)
    inst = INSTRUMENTS[0]
    for i in range(6):
        _feed(s, _bar(inst, i, 100.0 + i))
    assert s.score(inst.id) is not None
    assert s.signal_ready(inst.id) is False


def test_ready_once_the_whole_field_is_scored():
    s = _strategy(lookback=4, min_ranked=6)
    for i in range(5):
        _feed_all(s, i, _flat_prices())
    assert all(s.signal_ready(inst.id) for inst in INSTRUMENTS)


# ------------------------------ rebalance grid ------------------------------ #
def test_grid_is_derived_from_timestamps_not_per_instrument_counters():
    """The ten real instruments do not start on the same day. A counter-based
    grid would give each of them a private schedule, which is not a
    cross-sectional rule."""
    s = _strategy(rebalance_bars=3)
    late = INSTRUMENTS[1]
    # Same absolute hour, wildly different bar counts -> same verdict.
    assert s.is_rebalance_bar(_bar(INSTRUMENTS[0], 6, 100.0)) is True
    assert s.is_rebalance_bar(_bar(late, 6, 100.0)) is True
    assert s.is_rebalance_bar(_bar(INSTRUMENTS[0], 7, 100.0)) is False


def test_no_signal_off_the_rebalance_grid():
    s = _strategy(lookback=4, rebalance_bars=4, long_n=2, min_ranked=6)
    winners = {i.id: 100.0 for i in INSTRUMENTS}
    for i in range(5):
        sigs = _feed_all(s, i, winners)
        assert all(v is None for v in sigs.values())
    # Bar 6 is off the 4-bar grid; the field is ranked and warm, yet nothing fires.
    winners[INSTRUMENTS[0].id] = 200.0
    assert all(v is None for v in _feed_all(s, 6, winners).values())
    # Bar 8 is on the grid.
    assert _feed_all(s, 8, winners)[INSTRUMENTS[0].id] is not None


# ------------------------------ selection ----------------------------------- #
def _warm(s, base: float = 100.0, bars: int = 5) -> dict:
    prices = _flat_prices(base)
    for i in range(bars):
        _feed_all(s, i, prices)
    return prices


def test_only_the_top_n_go_long():
    s = _strategy(lookback=4, rebalance_bars=2, long_n=2, min_ranked=6)
    prices = _warm(s)
    # Bar 6 is on the 2-bar grid. Rank by the 4-bar return ending at bar 6.
    gains = [0.30, 0.20, 0.10, 0.05, 0.0, -0.10]
    for inst, g in zip(INSTRUMENTS, gains, strict=True):
        prices[inst.id] = 100.0 * (1 + g)
    sigs = _feed_all(s, 6, prices)
    longs = [iid for iid, sig in sigs.items() if sig and sig.direction is Direction.LONG]
    assert longs == [INSTRUMENTS[0].id, INSTRUMENTS[1].id]
    assert all(sigs[i.id] is None for i in INSTRUMENTS[2:])


def test_long_short_sets_are_disjoint_and_hit_both_ends():
    s = _strategy(
        lookback=4,
        rebalance_bars=2,
        long_n=2,
        short_n=2,
        min_ranked=6,
        max_concurrent_positions=4,
        risk_equity_pct=0.25,
    )
    prices = _warm(s)
    gains = [0.30, 0.20, 0.10, 0.05, -0.20, -0.30]
    for inst, g in zip(INSTRUMENTS, gains, strict=True):
        prices[inst.id] = 100.0 * (1 + g)
    sigs = _feed_all(s, 6, prices)
    assert sigs[INSTRUMENTS[0].id].direction is Direction.LONG
    assert sigs[INSTRUMENTS[1].id].direction is Direction.LONG
    assert sigs[INSTRUMENTS[2].id] is None
    assert sigs[INSTRUMENTS[3].id] is None
    assert sigs[INSTRUMENTS[5].id].direction is Direction.SHORT
    assert sigs[INSTRUMENTS[4].id].direction is Direction.SHORT


def test_reversal_control_inverts_the_ranking():
    """The control exists to separate 'no cross-sectional structure' from
    'structure with the opposite sign' -- so it must actually invert (R40)."""
    losers_first = _strategy(lookback=4, rebalance_bars=2, long_n=2, min_ranked=6, reversal=True)
    prices = _warm(losers_first)
    gains = [0.30, 0.20, 0.10, 0.05, 0.0, -0.10]
    for inst, g in zip(INSTRUMENTS, gains, strict=True):
        prices[inst.id] = 100.0 * (1 + g)
    sigs = _feed_all(losers_first, 6, prices)
    longs = {iid for iid, sig in sigs.items() if sig and sig.direction is Direction.LONG}
    assert longs == {INSTRUMENTS[5].id, INSTRUMENTS[4].id}


def test_ranking_order_does_not_depend_on_bar_arrival_order():
    """Ten symbols share a timestamp; which arrives first is a feed detail and
    must never reach a trading decision."""
    forwards, backwards = _strategy(min_ranked=6), _strategy(min_ranked=6)
    prices = _flat_prices()
    for i in range(5):
        for inst in INSTRUMENTS:
            _feed(forwards, _bar(inst, i, prices[inst.id]))
        for inst in reversed(INSTRUMENTS):
            _feed(backwards, _bar(inst, i, prices[inst.id]))
    assert forwards.ranked() == backwards.ranked()


# ------------------------------ lookahead (R8/F014) ------------------------- #
def test_decision_at_a_bar_cannot_see_the_next_bar():
    """The whole point. Two identical runs up to the rebalance bar, then one of
    them is given a later bar that would completely reorder the field: the
    decision already taken must be unchanged, and it must have used the ranking
    as of the rebalance bar only.
    """
    gains_at_6 = [0.30, 0.20, 0.10, 0.05, 0.0, -0.10]
    # At bar 8 the order is exactly reversed. If any future information leaked
    # into the bar-6 decision, the bar-6 longs would be the bar-8 winners.
    gains_at_8 = list(reversed(gains_at_6))

    s = _strategy(lookback=4, rebalance_bars=2, long_n=2, min_ranked=6)
    prices = _warm(s)
    for inst, g in zip(INSTRUMENTS, gains_at_6, strict=True):
        prices[inst.id] = 100.0 * (1 + g)
    decision_at_6 = _feed_all(s, 6, prices)

    for inst, g in zip(INSTRUMENTS, gains_at_8, strict=True):
        prices[inst.id] = 100.0 * (1 + g)
    _feed_all(s, 7, prices)
    decision_at_8 = _feed_all(s, 8, prices)

    at_6 = {i for i, sig in decision_at_6.items() if sig}
    at_8 = {i for i, sig in decision_at_8.items() if sig}
    assert at_6 == {INSTRUMENTS[0].id, INSTRUMENTS[1].id}
    assert at_8 == {INSTRUMENTS[5].id, INSTRUMENTS[4].id}
    assert at_6.isdisjoint(at_8), "the bar-6 pick anticipated the bar-8 ranking"


def test_signal_never_reads_the_current_bar_high_or_low():
    """A bar is transacted at its CLOSE (R8). A rule that consulted the current
    bar's high or low would be trading a price it could not have got."""
    s = _strategy(lookback=4, rebalance_bars=2, long_n=2, min_ranked=6)
    wild = _strategy(lookback=4, rebalance_bars=2, long_n=2, min_ranked=6)
    prices = _warm(s)
    _warm(wild)
    gains = [0.30, 0.20, 0.10, 0.05, 0.0, -0.10]
    for inst, g in zip(INSTRUMENTS, gains, strict=True):
        prices[inst.id] = 100.0 * (1 + g)

    plain = _feed_all(s, 6, prices)
    # Same closes, but every bar sweeps an enormous range around them.
    wild_sigs = {}
    for inst in INSTRUMENTS:
        close = prices[inst.id]
        bar = Bar(
            bar_type=BAR_TYPES[inst.id],
            open=inst.make_price(close),
            high=inst.make_price(close * 3.0),
            low=inst.make_price(close / 3.0),
            close=inst.make_price(close),
            volume=inst.make_qty(1),
            ts_event=T0 + 6 * H,
            ts_init=T0 + 6 * H,
        )
        wild.on_indicators_update(bar)
        wild_sigs[inst.id] = wild.on_bar_signal(bar)

    assert {i for i, v in plain.items() if v} == {i for i, v in wild_sigs.items() if v}


# ------------------------------ framework contract -------------------------- #
def test_rolling_state_is_built_in_on_indicators_update():
    """R46/F033: `on_bar_signal` is not called during warm-up replay, so state
    built there is empty for the first `warmup_bars()` live bars."""
    s = _strategy(lookback=4)
    inst = INSTRUMENTS[0]
    for i in range(6):
        s.on_indicators_update(_bar(inst, i, 100.0 + i))  # no on_bar_signal at all
    assert s.score(inst.id) is not None


def test_inherits_framework_defaults_unchanged():
    """A new strategy is a signal, not a system (R24)."""
    s = _strategy()
    assert s.limits.risk_equity_pct == 0.5
    assert s.limits.max_leverage == 3.0
    assert s.limits.max_concurrent_positions == 3
    assert s.exit_cfg.sl_atr_mult == 2.0
    assert s.exit_cfg.tsl_enabled is True
    assert s.exit_cfg.target_roi_pct is None  # ADR-0008
