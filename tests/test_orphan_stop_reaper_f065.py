"""F065 root cause: a close-position stop outlives the position it guarded.

Timeline measured on the demo box:

    2026-09-25 12:00  SOL long opens, close-position stop placed at 115.597
    2026-09-26 ~16:0x the position is flattened while the node is down
    2026-09-26 16:13  restart. ADOPTION close_journal SOL: "venue flat: closed
                      while offline" -- the journal row is closed and NOTHING is
                      sent to the venue
    2026-09-27 08:00  a new SOL long opens; its stop is refused, "You can only
                      place 1 TP/SL order to close an entire position"

The stop from the 25th was still live. It is invisible to everything we own:
nautilus's reconciliation does not load OKX algo orders, so it is not in the
order cache, so `stale_stops()` cannot see it and `_sweep_stale_stops` cannot
cancel it. Only OKX knows, and OKX only tells us by refusing the next stop.

`_adopt_positions` peeks at the venue for instruments that HOLD a position. An
instrument that is flat is never looked at -- which is exactly where an orphan
lives.

So: at start, cancel our close-position stops on every instrument the venue says
is flat. A stop guarding nothing has no purpose and blocks the next trade's.

Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ntx.ops import venue_stops

SOL = "SOL-USDT-SWAP"
LINK = "LINK-USDT-SWAP"
TRX = "TRX-USDT-SWAP"


def _stop(inst, algo_id, px="100"):
    return {
        "instId": inst,
        "algoId": algo_id,
        "slTriggerPx": px,
        "reduceOnly": "true",
        "closeFraction": "1",
    }


def test_a_stop_on_a_flat_instrument_is_an_orphan():
    pending = [_stop(SOL, "a1", "115.597"), _stop(TRX, "a2", "0.3366")]
    found = venue_stops.orphans(pending, held={f"{TRX}.OKX"})
    assert [o["algoId"] for o in found] == ["a1"]


def test_a_stop_on_a_held_instrument_is_never_reaped():
    pending = [_stop(TRX, "a2")]
    assert venue_stops.orphans(pending, held={f"{TRX}.OKX"}) == []


def test_the_suffix_is_not_load_bearing():
    """Our ids carry `.OKX`; the venue's do not. A mismatch would reap live stops."""
    pending = [_stop(TRX, "a2")]
    assert venue_stops.orphans(pending, held={TRX}) == []


def test_an_order_that_is_not_our_close_position_stop_is_left_alone():
    """Somebody else's trigger order is not ours to cancel (H-01)."""
    foreign = {"instId": SOL, "algoId": "x9", "tpTriggerPx": "130", "reduceOnly": "false"}
    assert venue_stops.orphans([foreign], held=set()) == []


def test_every_flat_instrument_is_swept_not_just_the_journalled_one():
    """LINK closed the same way as SOL and left the same orphan behind."""
    pending = [_stop(SOL, "a1"), _stop(LINK, "a3"), _stop(TRX, "a2")]
    found = venue_stops.orphans(pending, held={f"{TRX}.OKX"})
    assert sorted(o["algoId"] for o in found) == ["a1", "a3"]


# ---- the strategy calls the reaper at start ------------------------------- #


def _strategy_with_reaper(*, held, writes_allowed=True):
    from ntx.strategies.base import NtxStrategy

    reaped: list = []
    fake = SimpleNamespace(
        instruments=dict.fromkeys(held or [f"{SOL}.OKX"]),
        journal=None,
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: None,
            debug=lambda *a: None,
        ),
        _jevent=lambda *a, **k: None,
        _venue_writes_allowed=lambda why: writes_allowed,
        venue_stop_reap_hook=lambda h: reaped.append(set(h)) or ["a1"],
    )
    NtxStrategy._reap_orphan_stops.__get__(fake, NtxStrategy)(held)
    return reaped


def test_the_reaper_runs_at_start_with_the_instruments_that_hold_a_position():
    reaped = _strategy_with_reaper(held={f"{TRX}.OKX"})
    assert reaped == [{f"{TRX}.OKX"}]


def test_observe_mode_reaps_nothing():
    """R58: observe issues NO venue command, and a cancel is a venue command."""
    assert _strategy_with_reaper(held={f"{TRX}.OKX"}, writes_allowed=False) == []


def test_a_reaper_that_fails_does_not_stop_the_node():
    """A stop we could not cancel is a refused entry later, not a dead node."""
    from ntx.strategies.base import NtxStrategy

    def boom(held):
        raise RuntimeError("venue timeout")

    events: list = []
    fake = SimpleNamespace(
        instruments={f"{SOL}.OKX": None},
        journal=None,
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: None,
            debug=lambda *a: None,
        ),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"))),
        _venue_writes_allowed=lambda why: True,
        venue_stop_reap_hook=boom,
    )
    NtxStrategy._reap_orphan_stops.__get__(fake, NtxStrategy)(set())
    assert ("orphan_stop_sweep_failed", "warning") in events


def test_orphans_rejects_a_held_set_that_is_not_iterable_of_strings():
    with pytest.raises((TypeError, AttributeError)):
        venue_stops.orphans([_stop(SOL, "a1")], held=None)
