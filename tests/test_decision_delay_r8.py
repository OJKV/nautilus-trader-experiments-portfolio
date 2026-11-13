"""docs/09 §7 latency stress (R8c; review 2026-10-11 F-04): decisions made on bar t
are executed on bar t+1 when `decision_delay_bars=1`. Default 0 changes nothing.

Written before the switch existed (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy
from ntx.strategies.signal import Direction, Signal

INST = "BTC-USDT-SWAP.OKX"


def _fake(delay, signal_dir=Direction.LONG):
    calls = []
    fake = SimpleNamespace(
        _last_live_bar_ns={INST: 0}, clock=SimpleNamespace(timestamp_ns=lambda: 1),
        _live_bars=0, _live_buffer={}, _ingest=lambda bar: True, _record_equity=lambda bar: None,
        bars_seen={INST: 999}, warmup_bars=lambda: 1,
        _pending_signal=Signal(INST, signal_dir) if signal_dir else None,
        config=SimpleNamespace(allow_short=True, decision_delay_bars=delay),
        states={}, exit_cfg=None, _pending_entry={}, _delayed={},
        _enter=lambda inst, d, bar: calls.append(("enter", d, bar.n)),
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
    )  # fmt: skip
    return fake, calls


def _bar(n):
    return SimpleNamespace(
        n=n, close=100.0, ts_event=n,
        bar_type=SimpleNamespace(instrument_id=INST),
    )  # fmt: skip


def test_without_delay_the_entry_happens_on_the_signal_bar():
    fake, calls = _fake(0)
    NtxStrategy.on_bar(fake, _bar(1))
    assert calls == [("enter", Direction.LONG, 1)]


def test_with_a_one_bar_delay_the_entry_happens_on_the_next_bar():
    fake, calls = _fake(1)
    NtxStrategy.on_bar(fake, _bar(1))
    assert calls == []
    fake._pending_signal = None  # no new signal on bar 2
    NtxStrategy.on_bar(fake, _bar(2))
    assert calls == [("enter", Direction.LONG, 2)]
