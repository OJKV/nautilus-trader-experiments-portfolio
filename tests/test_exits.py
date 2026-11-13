"""Exit-stack tests.

These rules decide when money comes off the table, so they are tested
exhaustively and without an engine. Each test names the rule or doc section it
protects.
"""

from __future__ import annotations

import pytest

from ntx.strategies.exits import (
    ExitConfig,
    PositionState,
    TrailingStop,
    evaluate_exit,
    initial_stop_price,
)
from ntx.strategies.signal import Direction, ExitReason, Signal

ENTRY = 100.0


def _long(stop=96.0, margin=10.0, units=1.0, trailing=None, bars=0) -> PositionState:
    return PositionState(Direction.LONG, ENTRY, stop, margin, bars, trailing, units)


def _short(stop=104.0, margin=10.0, units=1.0, trailing=None, bars=0) -> PositionState:
    return PositionState(Direction.SHORT, ENTRY, stop, margin, bars, trailing, units)


# --------------------------------- config ----------------------------------- #
def test_stop_loss_is_mandatory():
    with pytest.raises(ValueError, match="stop loss is mandatory"):
        ExitConfig(sl_atr_mult=None, sl_price_pct=None)


def test_cannot_set_both_stop_modes():
    with pytest.raises(ValueError, match="exactly one"):
        ExitConfig(sl_atr_mult=2.0, sl_price_pct=2.0)


def test_tsl_percentages_must_be_positive():
    with pytest.raises(ValueError, match="tsl_trail_price_pct"):
        ExitConfig(tsl_trail_price_pct=0.0)


def test_signal_strength_is_bounded():
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        Signal("X.OKX", Direction.LONG, strength=1.5)


# ------------------------------ initial stop -------------------------------- #
def test_atr_stop_long_and_short():
    cfg = ExitConfig(sl_atr_mult=2.0)
    assert initial_stop_price(Direction.LONG, 100.0, cfg, atr=2.0) == 96.0
    assert initial_stop_price(Direction.SHORT, 100.0, cfg, atr=2.0) == 104.0


def test_fixed_pct_stop():
    cfg = ExitConfig(sl_atr_mult=None, sl_price_pct=2.0)
    assert initial_stop_price(Direction.LONG, 100.0, cfg, atr=None) == 98.0
    assert initial_stop_price(Direction.SHORT, 100.0, cfg, atr=None) == 102.0


def test_atr_stop_requires_positive_atr():
    """A flat synthetic series gives ATR 0; that must raise, not place the stop
    at the entry price."""
    cfg = ExitConfig(sl_atr_mult=2.0)
    for atr in (None, 0.0, -1.0):
        with pytest.raises(ValueError, match="positive ATR"):
            initial_stop_price(Direction.LONG, 100.0, cfg, atr=atr)


def test_atr_stop_scales_with_volatility():
    """Why ATR and not a fixed %: a 2% stop is too tight for DOGE, too loose for
    BTC. Identical config must give different distances per asset."""
    cfg = ExitConfig(sl_atr_mult=2.0)
    calm = initial_stop_price(Direction.LONG, 100.0, cfg, atr=0.5)
    wild = initial_stop_price(Direction.LONG, 100.0, cfg, atr=5.0)
    assert 100.0 - calm == 1.0
    assert 100.0 - wild == 10.0


# ------------------------------ trailing stop ------------------------------- #
def test_trailing_stop_arms_only_after_activation():
    t = TrailingStop(Direction.LONG, ENTRY, activation_pct=1.5, trail_pct=1.0)
    assert t.update(100.5) is None
    assert t.update(101.4) is None
    assert not t.armed
    assert t.update(101.6) is not None
    assert t.armed


def test_trailing_stop_is_monotonic_long():
    """R: a trailing stop must never move against the position. One that can
    loosen is just a way to give profit back."""
    t = TrailingStop(Direction.LONG, ENTRY, 1.5, 1.0)
    levels = []
    for p in (102.0, 105.0, 110.0, 104.0, 103.0, 108.0, 99.0):
        lvl = t.update(p)
        if lvl is not None:
            levels.append(lvl)
    assert levels == sorted(levels), f"level moved down: {levels}"


def test_trailing_stop_is_monotonic_short():
    t = TrailingStop(Direction.SHORT, ENTRY, 1.5, 1.0)
    levels = []
    for p in (98.0, 95.0, 90.0, 96.0, 97.0, 92.0, 101.0):
        lvl = t.update(p)
        if lvl is not None:
            levels.append(lvl)
    assert levels == sorted(levels, reverse=True), f"level moved up: {levels}"


def test_trailing_level_tracks_best_price_not_last():
    t = TrailingStop(Direction.LONG, ENTRY, 1.5, 1.0)
    t.update(110.0)
    t.update(104.0)
    assert t.best_price == 110.0
    assert t.level == pytest.approx(110.0 * 0.99)


def test_trailing_breach_detection_long_and_short():
    t = TrailingStop(Direction.LONG, ENTRY, 1.5, 1.0)
    t.update(110.0)  # level 108.9
    assert not t.is_breached(109.0)
    assert t.is_breached(108.9)
    assert t.is_breached(100.0)

    s = TrailingStop(Direction.SHORT, ENTRY, 1.5, 1.0)
    s.update(90.0)  # level 90.9
    assert not s.is_breached(90.5)
    assert s.is_breached(90.9)


def test_unarmed_trailing_never_breaches():
    t = TrailingStop(Direction.LONG, ENTRY, 1.5, 1.0)
    t.update(100.2)
    assert not t.armed
    assert not t.is_breached(90.0), "an unarmed trail must not trigger"


def test_trailing_stop_needs_a_direction():
    with pytest.raises(ValueError, match="needs a direction"):
        TrailingStop(Direction.FLAT, ENTRY, 1.5, 1.0)


# ------------------------------ exit ordering ------------------------------- #
def test_opposite_signal_exits_first():
    cfg = ExitConfig()
    st = _long()
    assert (
        evaluate_exit(st, 101.0, cfg, signal_direction=Direction.SHORT)
        is ExitReason.OPPOSITE_SIGNAL
    )


def test_same_signal_does_not_exit():
    cfg = ExitConfig()
    assert evaluate_exit(_long(), 101.0, cfg, signal_direction=Direction.LONG) is None


def test_stop_loss_beats_trailing_when_both_breached():
    """Pessimistic attribution: a bar breaching both is a stop-out, which is how
    a venue evaluating both orders on one tick behaves."""
    cfg = ExitConfig()
    t = TrailingStop(Direction.LONG, ENTRY, 1.5, 1.0)
    t.update(110.0)  # trail level 108.9
    st = _long(stop=96.0, trailing=t)
    assert evaluate_exit(st, 95.0, cfg) is ExitReason.STOP_LOSS


def test_stop_loss_triggers_long_and_short():
    cfg = ExitConfig()
    assert evaluate_exit(_long(stop=96.0), 96.0, cfg) is ExitReason.STOP_LOSS
    assert evaluate_exit(_long(stop=96.0), 96.01, cfg) is None
    assert evaluate_exit(_short(stop=104.0), 104.0, cfg) is ExitReason.STOP_LOSS
    assert evaluate_exit(_short(stop=104.0), 103.99, cfg) is None


def test_trailing_stop_exit():
    cfg = ExitConfig()
    t = TrailingStop(Direction.LONG, ENTRY, 1.5, 1.0)
    st = _long(trailing=t)
    assert evaluate_exit(st, 105.0, cfg) is None  # arms, level 103.95
    assert evaluate_exit(st, 103.9, cfg) is ExitReason.TRAILING_STOP


def test_trailing_disabled_does_nothing():
    cfg = ExitConfig(tsl_enabled=False)
    t = TrailingStop(Direction.LONG, ENTRY, 1.5, 1.0)
    st = _long(trailing=t)
    evaluate_exit(st, 105.0, cfg)
    assert evaluate_exit(st, 103.9, cfg) is None


# --------------------------- price vs ROI semantics ------------------------- #
def test_take_profit_is_a_price_move():
    cfg = ExitConfig(tsl_enabled=False, tp_price_pct=3.0)
    assert evaluate_exit(_long(), 102.9, cfg) is None
    assert evaluate_exit(_long(), 103.0, cfg) is ExitReason.TAKE_PROFIT


def test_roi_target_is_return_on_margin_not_price():
    """The R4 bug in one test: at 3x leverage a 5% ROI is a 1.67% price move.

    100 base units at entry 100 = $10,000 notional on $3,333 margin (3x). A 1%
    price move is $100 profit = 3% ROI.
    """
    cfg = ExitConfig(tsl_enabled=False, target_roi_pct=3.0)
    st = PositionState(Direction.LONG, 100.0, 96.0, margin_usd=3_333.33, base_units=100.0)
    assert st.price_move_pct(101.0) == pytest.approx(1.0)
    assert st.roi_pct(101.0) == pytest.approx(3.0, rel=1e-3)
    assert evaluate_exit(st, 100.9, cfg) is None
    assert evaluate_exit(st, 101.0, cfg) is ExitReason.ROI_TARGET


def test_roi_10_to_15_pct_needs_a_real_price_move_at_3x():
    """Why the spec moved from 3-5% to 10-15% (ADR-0008).

    At 3x leverage the ROI target divides by 3 to give the required price move:
    a 5% ROI fires on 1.67% -- noise in crypto -- while 10-15% needs 3.3-5.0%,
    which is a real move. Prices are derived from the ROI rather than written as
    rounded literals, so the boundary is exact.
    """
    margin, units, entry = 3_333.33, 100.0, 100.0
    st = PositionState(Direction.LONG, entry, 96.0, margin_usd=margin, base_units=units)
    leverage = units * entry / margin  # ~3.0

    for roi in (5.0, 10.0, 15.0):
        cfg = ExitConfig(tsl_enabled=False, target_roi_pct=roi)
        required_move_pct = roi / leverage
        assert required_move_pct == pytest.approx({5.0: 1.67, 10.0: 3.33, 15.0: 5.0}[roi], abs=0.01)
        just_over = entry * (1 + (required_move_pct + 0.01) / 100.0)
        just_under = entry * (1 + (required_move_pct - 0.01) / 100.0)
        assert evaluate_exit(st, just_over, cfg) is ExitReason.ROI_TARGET
        assert evaluate_exit(st, just_under, cfg) is None


def test_roi_with_zero_margin_is_zero_not_infinite():
    st = PositionState(Direction.LONG, 100.0, 96.0, margin_usd=0.0, base_units=1.0)
    assert st.roi_pct(200.0) == 0.0


def test_short_side_pnl_and_roi_signs():
    st = PositionState(Direction.SHORT, 100.0, 104.0, margin_usd=100.0, base_units=10.0)
    assert st.price_move_pct(95.0) == pytest.approx(5.0)  # favourable
    assert st.unrealised_pnl_usd(95.0) == pytest.approx(50.0)
    assert st.roi_pct(95.0) == pytest.approx(50.0)
    assert st.price_move_pct(105.0) == pytest.approx(-5.0)  # adverse
    assert st.unrealised_pnl_usd(105.0) == pytest.approx(-50.0)


# -------------------------------- time stop --------------------------------- #
def test_time_stop():
    cfg = ExitConfig(tsl_enabled=False, max_bars_in_position=10)
    assert evaluate_exit(_long(bars=9), 101.0, cfg) is None
    assert evaluate_exit(_long(bars=10), 101.0, cfg) is ExitReason.TIME_STOP


def test_defaults_are_trend_following():
    """ADR-0008: cross + SL + TSL on; TP, ROI and time stop off."""
    cfg = ExitConfig()
    assert cfg.tsl_enabled is True
    assert cfg.sl_atr_mult == 2.0
    assert cfg.tp_price_pct is None
    assert cfg.target_roi_pct is None
    assert cfg.max_bars_in_position is None


def test_flat_position_never_exits():
    st = PositionState(Direction.FLAT, 100.0, 0.0, 0.0)
    assert evaluate_exit(st, 50.0, ExitConfig()) is None


# ------------------ S001 filter wiring (F023 regression) -------------------- #
def test_filters_are_actually_consulted():
    """F023: the filters were implemented but never called from the signal path.

    Every dx_min value produced byte-identical results, which looked like "the
    filter does not help" and was in fact "the filter never ran". A veto must be
    observable.
    """
    import inspect

    from ntx.strategies.ma_cross_trend import MACrossTrendStrategy

    src = inspect.getsource(MACrossTrendStrategy.on_bar_signal)
    assert "_passes_filters" in src, "on_bar_signal must consult the filters"
    assert "filtered_out" in src, "a veto must be counted so a no-op is visible"


def test_regime_filter_blocks_wrong_side():
    """Long only above the regime MA, short only below."""
    from ntx.strategies.ma_cross_trend import MACrossTrendConfig, MACrossTrendStrategy

    s = MACrossTrendStrategy(
        MACrossTrendConfig(instrument_ids=["BTC-USDT-SWAP.OKX"], regime_period=200)
    )

    class _MA:
        initialized = True
        value = 100.0

    inst = next(iter(s.instrument_ids))
    s.regime[inst] = _MA()
    assert s._passes_filters(inst, Direction.LONG, 101.0)
    assert not s._passes_filters(inst, Direction.LONG, 99.0)
    assert s._passes_filters(inst, Direction.SHORT, 99.0)
    assert not s._passes_filters(inst, Direction.SHORT, 101.0)


def test_uninitialised_regime_filter_blocks_entry():
    """Fail closed: an un-warmed filter must not wave trades through."""
    from ntx.strategies.ma_cross_trend import MACrossTrendConfig, MACrossTrendStrategy

    s = MACrossTrendStrategy(
        MACrossTrendConfig(instrument_ids=["BTC-USDT-SWAP.OKX"], regime_period=200)
    )

    class _MA:
        initialized = False
        value = 0.0

    inst = next(iter(s.instrument_ids))
    s.regime[inst] = _MA()
    assert not s._passes_filters(inst, Direction.LONG, 101.0)


def test_dx_filter_blocks_when_below_threshold():
    from ntx.strategies.ma_cross_trend import MACrossTrendConfig, MACrossTrendStrategy

    s = MACrossTrendStrategy(MACrossTrendConfig(instrument_ids=["BTC-USDT-SWAP.OKX"], dx_min=25.0))

    class _DM:
        initialized = True

        def __init__(self, pos, neg):
            self.pos, self.neg = pos, neg

    inst = next(iter(s.instrument_ids))
    s.dm[inst] = _DM(30.0, 10.0)  # DX = 20/40*100 = 50 -> passes
    assert s._passes_filters(inst, Direction.LONG, 100.0)
    s.dm[inst] = _DM(21.0, 20.0)  # DX = 1/41*100 = 2.4 -> blocked
    assert not s._passes_filters(inst, Direction.LONG, 100.0)


def test_no_filters_configured_passes_everything():
    from ntx.strategies.ma_cross_trend import MACrossTrendConfig, MACrossTrendStrategy

    s = MACrossTrendStrategy(MACrossTrendConfig(instrument_ids=["BTC-USDT-SWAP.OKX"]))
    inst = next(iter(s.instrument_ids))
    assert s._passes_filters(inst, Direction.LONG, 100.0)
    assert s._passes_filters(inst, Direction.SHORT, 100.0)
