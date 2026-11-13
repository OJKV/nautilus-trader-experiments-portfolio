"""S003 Donchian breakout tests.

The critical property is that the channel describes **prior** bars only. A
Donchian channel that includes the live bar makes `close > channel_high` nearly
tautological, because the close contributed to that high -- which silently turns
a breakout rule into a near-random one.
"""

from __future__ import annotations

import pandas as pd
import pytest
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.signal import Direction

INST = TestInstrumentProvider.ethusdt_binance()
BT = BarType.from_str(f"{INST.id}-1-HOUR-LAST-EXTERNAL")
H = 3_600_000_000_000
T0 = int(pd.Timestamp("2024-01-01", tz="UTC").value)


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


def _strategy(**kw) -> DonchianBreakoutStrategy:
    cfg = DonchianBreakoutConfig(instrument_ids=[str(INST.id)], **kw)
    s = DonchianBreakoutStrategy(cfg)
    s.on_indicators_init(INST.id)
    return s


# ------------------------------- guards ------------------------------------- #
def test_entry_period_minimum():
    with pytest.raises(ValueError, match="entry_period must be >= 2"):
        _strategy(entry_period=1)


def test_exit_period_must_be_narrower_than_entry():
    with pytest.raises(ValueError, match="never closes"):
        _strategy(entry_period=20, exit_period=20)


def test_warmup_covers_entry_and_atr():
    assert _strategy(entry_period=55, atr_period=14).warmup_bars() == 56
    # exit_period must stay below entry_period, so it is set explicitly here
    assert _strategy(entry_period=10, exit_period=5, atr_period=100).warmup_bars() == 101


def _feed(s, bar):
    """The real pipeline for one bar: `_ingest` records it via
    `on_indicators_update` (history included, so the channel is usable straight
    after warm-up -- R-01) and only then asks for a signal."""
    s.on_indicators_update(bar)
    return s.on_bar_signal(bar)


# --------------------- the channel must exclude the live bar ---------------- #
def test_channel_uses_prior_bars_only():
    """The load-bearing property. Feed a flat range, then one bar that makes a
    huge new high: it must be a breakout, which is only possible if the channel
    was computed BEFORE that bar was recorded.
    """
    s = _strategy(entry_period=5, exit_period=2)
    for i in range(5):
        assert _feed(s, _bar(i, 101.0, 99.0, 100.0)) is None  # warming
    sig = _feed(s, _bar(5, 120.0, 99.0, 115.0))
    assert sig is not None and sig.direction is Direction.LONG


def test_no_signal_before_channel_is_full():
    s = _strategy(entry_period=10, exit_period=3)
    for i in range(9):
        assert _feed(s, _bar(i, 100.0 + i, 90.0, 95.0 + i)) is None


def test_a_rising_close_inside_the_range_is_not_a_breakout():
    s = _strategy(entry_period=5, exit_period=2)
    for i in range(6):
        _feed(s, _bar(i, 110.0, 90.0, 100.0))
    assert _feed(s, _bar(6, 109.0, 95.0, 108.0)) is None, "inside the channel"


def test_break_down_gives_short():
    s = _strategy(entry_period=5, exit_period=2)
    for i in range(5):
        _feed(s, _bar(i, 101.0, 99.0, 100.0))
    sig = _feed(s, _bar(5, 101.0, 80.0, 85.0))
    assert sig is not None and sig.direction is Direction.SHORT


def test_channel_returns_none_until_enough_history():
    """`period` PRIOR bars, so the bar being judged is not in its own channel:
    5 recorded bars is not enough, 6 is (R-01 changed where they are recorded,
    not what the channel means)."""
    s = _strategy(entry_period=5, exit_period=2)
    assert s.channel(INST.id, 5) is None
    for i in range(5):
        _feed(s, _bar(i, 100.0 + i, 90.0 - i, 95.0))
    assert s.channel(INST.id, 5) is None
    _feed(s, _bar(5, 100.0, 90.0, 95.0))
    ch = s.channel(INST.id, 5)
    assert ch is not None
    hi, lo = ch
    assert hi == pytest.approx(104.0)
    assert lo == pytest.approx(86.0)


def test_history_is_bounded():
    """Rolling buffers must not grow without limit over a multi-year backtest."""
    s = _strategy(entry_period=20, exit_period=10)
    for i in range(500):
        _feed(s, _bar(i, 101.0, 99.0, 100.0))
    assert len(s._highs[INST.id]) <= 22
    assert len(s._lows[INST.id]) <= 22


def test_exit_break_can_be_disabled():
    s = _strategy(entry_period=5, exit_period=2, use_exit_break=False)
    for i in range(6):
        _feed(s, _bar(i, 101.0, 99.0, 100.0))
    # no position is held, so an exit break is irrelevant either way
    assert _feed(s, _bar(6, 100.5, 99.5, 100.0)) is None


def test_registry_exposes_s003_and_rejects_unknown():
    from ntx.strategies.registry import get

    cls, cfg_cls = get("S003")
    assert cls is DonchianBreakoutStrategy
    assert cfg_cls is DonchianBreakoutConfig
    with pytest.raises(KeyError, match="unknown strategy"):
        get("S999")


def test_inherits_framework_defaults_unchanged():
    """A new strategy is a signal, not a system (R24): it must pick up the risk
    layer and exit stack without redefining them."""
    s = _strategy()
    assert s.limits.risk_equity_pct == 0.5  # user's choice, 2026-09-16
    assert s.limits.max_leverage == 3.0
    assert s.exit_cfg.sl_atr_mult == 2.0
    assert s.exit_cfg.tsl_enabled is True
    assert s.exit_cfg.target_roi_pct is None  # ADR-0008
