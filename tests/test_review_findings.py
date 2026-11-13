"""External code review (docs/antigravity/code_review.md), 2026-09-17.

Each finding judged reproducible is pinned here by a test written BEFORE the
fix (R41). Findings judged not reproducible or by-design are recorded in
docs/05-failure-log.md (R-002) with the reason.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from test_live_path import _engine, _fake_accounting, _fill2, _row

from ntx.strategies.base import NtxStrategy
from ntx.strategies.exits import PositionState
from ntx.strategies.rehearsal import RehearsalConfig, RehearsalStrategy
from ntx.strategies.signal import Direction
from ntx.strategies.watchdog import stale_stops


# ------------------------------------------------ C1: stop partial fills ---- #
def test_stop_partial_fills_are_all_accounted(tmp_path):
    """A stop-market can fill in pieces. Only the first piece was journalled;
    later pieces were ignored because the stop id had already been dropped."""
    fake, _calls, j = _fake_accounting(tmp_path)
    NtxStrategy.on_order_filled(fake, _fill2("entry", 3.0, 100.0, 0.15))
    NtxStrategy.on_order_filled(fake, _fill2("stop-0", 1.0, 96.0, 0.05, side="SELL"))
    NtxStrategy.on_order_filled(fake, _fill2("stop-0", 2.0, 93.0, 0.10, side="SELL"))
    r = _row(j, "t2")
    assert r["state"] == "closed"
    assert r["exit_reason"] == "stop_loss"
    assert r["exit_price"] == pytest.approx(94.0)  # vwap 96x1 + 93x2
    assert r["fees_usd"] == pytest.approx(0.30)
    assert r["pnl_net_usd"] == pytest.approx(-18.30)


# ---------------------------------------------- H1: flip ordering ---------- #
def _flip_fake(open_positions):
    calls = SimpleNamespace(entered=[], cancelled=[])
    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, meta={"filled_qty": 1.0})
    fake = SimpleNamespace(
        states={"X": st},
        _pending_entry={},
        cache=SimpleNamespace(positions_open=lambda instrument_id=None: list(open_positions)),
        _enter=lambda inst_id, d, bar: calls.entered.append((inst_id, d)),
        _cancel_protective_stop=lambda inst_id: calls.cancelled.append(inst_id),
        # An externally closed position also closes its journal row (F047);
        # that path has its own test in test_single_instance.py.
        _close_journal_trade=lambda *a, **k: None,
        journal=None,
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
    )
    return fake, calls


def test_flip_waits_for_the_old_position_to_close():
    """Live: the close confirmation arrives AFTER the new entry, and
    on_position_closed then deleted the NEW position's state and cancelled its
    stop. The re-entry must wait for the close."""
    fake, calls = _flip_fake(open_positions=["pos"])
    NtxStrategy._flip(fake, "X", Direction.SHORT, "bar")
    assert calls.entered == []
    assert "X" in fake._pending_entry
    NtxStrategy.on_position_closed(fake, SimpleNamespace(instrument_id="X"))
    assert calls.entered == [("X", Direction.SHORT)]
    assert "X" not in fake._pending_entry


def test_flip_enters_at_once_when_already_flat():
    fake, calls = _flip_fake(open_positions=[])
    NtxStrategy._flip(fake, "X", Direction.SHORT, "bar")
    assert calls.entered == [("X", Direction.SHORT)]


def test_position_closed_does_not_drop_an_unfilled_new_entry():
    fake, calls = _flip_fake(open_positions=[])
    fake.states["X"].meta["filled_qty"] = 0.0  # entry submitted, not yet filled
    NtxStrategy.on_position_closed(fake, SimpleNamespace(instrument_id="X"))
    assert "X" in fake.states
    assert calls.cancelled == []


def test_position_closed_clears_a_filled_position():
    fake, calls = _flip_fake(open_positions=[])
    NtxStrategy.on_position_closed(fake, SimpleNamespace(instrument_id="X"))
    assert "X" not in fake.states
    assert calls.cancelled == ["X"]


# ------------------------------------------- H2: notional at mark ---------- #
def test_open_notional_uses_current_price():
    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, base_units=2.0)
    fake = SimpleNamespace(states={"X": st}, _last_close=lambda inst_id: 150.0)
    assert NtxStrategy._open_notional_usd(fake) == pytest.approx(300.0)


def test_open_notional_falls_back_to_entry_without_a_price():
    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, base_units=2.0)
    fake = SimpleNamespace(states={"X": st}, _last_close=lambda inst_id: None)
    assert NtxStrategy._open_notional_usd(fake) == pytest.approx(200.0)


# ----------------------------------------- L2: orphaned stops -------------- #
def test_stale_stops_are_open_stops_nobody_tracks():
    assert stale_stops(["a", "b", "c"], {"b"}) == ["a", "c"]
    assert stale_stops([], {"b"}) == []
    assert stale_stops(["b"], {"b"}) == []


def test_watchdog_sweeps_stale_stops():
    import inspect

    src = inspect.getsource(NtxStrategy._protection_watchdog)
    assert "stale_stops" in src


# ----------------------------------------- C4: equity timestamp mixing ----- #
def _bars(inst, bt, closes, start_ns):
    h = 3_600_000_000_000
    return [
        Bar(
            bar_type=bt,
            open=inst.make_price(c),
            high=inst.make_price(c * 1.001),
            low=inst.make_price(c * 0.999),
            close=inst.make_price(c),
            volume=inst.make_qty(10),
            ts_event=start_ns + i * h,
            ts_init=start_ns + i * h,
        )
        for i, c in enumerate(closes)
    ]


def test_equity_sample_uses_prices_from_one_timestamp():
    """Equity was sampled when the FIRST instrument's bar of a timestamp
    arrived, so the other instruments were still at the previous bar's price."""
    import pandas as pd

    a = TestInstrumentProvider.ethusdt_binance()
    b = TestInstrumentProvider.btcusdt_binance()
    engine = _engine(a)
    engine.add_instrument(b)
    start = int(pd.Timestamp("2024-01-01", tz="UTC").value)
    n, jump_at = 40, 30
    bta = BarType.from_str(f"{a.id}-1-HOUR-LAST-EXTERNAL")
    btb = BarType.from_str(f"{b.id}-1-HOUR-LAST-EXTERNAL")
    engine.add_data(_bars(a, bta, [2000.0] * n, start))  # A is flat
    b_closes = [40000.0] * jump_at + [44000.0] * (n - jump_at)  # B jumps +10%
    engine.add_data(_bars(b, btb, b_closes, start))
    s = RehearsalStrategy(
        RehearsalConfig(
            instrument_ids=[str(a.id), str(b.id)],
            bar_spec="1-HOUR-LAST-EXTERNAL",
            hold_bars=1000,
            sl_atr_mult=None,
            sl_price_pct=50.0,
        )
    )
    engine.add_strategy(s)
    engine.run()
    engine.dispose()
    curve = dict(s.equity_curve)
    h = 3_600_000_000_000
    t_jump = start + jump_at * h
    assert t_jump in curve and (t_jump - h) in curve
    b_units = s.states[b.id].base_units
    expected = b_units * 4000.0
    assert curve[t_jump] - curve[t_jump - h] == pytest.approx(expected, rel=0.02), (
        "the jump in B must show at the timestamp it happened"
    )
