"""L2-04: one position adopted per event-loop turn, because a submit is not a send.

`_submit_stop` -> `submit_order` -> nautilus's `LiveRiskEngine.execute`, which
only **enqueues**. The queue is drained by a task on the same event loop that
`on_start` is blocking. So releasing every position's venue stop inside
`on_start` produces, with positions A, B and C:

    cancel A, queue A, cancel B, queue B, cancel C, queue C, return
    ...only now does anything actually reach the venue.

A is unprotected for the whole of B's and C's REST round trips -- up to 5 signed
calls each -- which is exactly what L-10 was supposed to end. And if C's release
raises, `on_start` raises and A's queued stop may never be sent at all.

The earlier test for L-10 asserted the *call order* on a stub, which this defect
satisfies perfectly. This one models the queue: `_submit_stop` enqueues, and only
`drain()` -- the event loop getting control back -- sends. The contract is that no
position's stop is cancelled while another's replacement is still sitting in the
queue.

Written before the fix (R41).
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from ntx.strategies.base import NtxStrategy


def _fake(instruments: list[str]):
    """A strategy stub whose venue only receives what the event loop drains."""
    order: list[tuple] = []
    queued: list[str] = []
    sent: list[str] = []
    timers: dict[str, object] = {}

    def drain():
        for inst in queued:
            sent.append(inst)
            order.append(("SENT", inst))
        queued.clear()

    positions = {
        i: SimpleNamespace(signed_qty=1.0, avg_px_open=100.0, quantity=1.0) for i in instruments
    }
    fake = SimpleNamespace(
        instruments={i: SimpleNamespace(multiplier=1.0) for i in instruments},
        instrument_ids=list(instruments),
        states={},
        stop_orders={},
        stop_client_order_ids={},
        journal=None,
        atr={},
        exit_cfg=SimpleNamespace(tsl_enabled=False),
        config=SimpleNamespace(trading_enabled=True, max_leverage=3.0),
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: None,
            debug=lambda *a: None,
        ),
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: (
                [positions[instrument_id]] if instrument_id in positions else []
            ),
            orders_open=lambda instrument_id=None: [],
        ),
        venue_stop_peek_hook=lambda i: (order.append(("peek", i)), 95.0)[1],
        venue_stop_hook=lambda i: (order.append(("release", i)), 95.0)[1],
        _submit_stop=lambda inst, state, qty: (
            order.append(("queue", inst)),
            queued.append(inst),
        )[0],
        _flatten=lambda inst: order.append(("flatten", inst)),
        _stop_attached_at_entry=lambda: False,
        clock=SimpleNamespace(
            set_timer=lambda name, interval, callback: timers.__setitem__(name, callback),
            cancel_timer=lambda name: timers.pop(name, None),
        ),
        id="S003",
    )
    fake.ADOPT_TICK_SECS = NtxStrategy.ADOPT_TICK_SECS
    fake._adopt_queue = []
    fake._adopt_timer_on = False
    for n in (
        "_adopt",
        "_jwrite",
        "_jevent",
        "_drain_adoptions",
        "_schedule_adoption_timer",
        "_cancel_adoption_timer",
        # F065: adoption reaps stops that guard no position before it plans.
        "_reap_orphan_stops",
        # F066: a "closed while offline" is checked at the venue first.
        "_confirm_venue_flat",
        "_venue_checked",
    ):
        setattr(fake, n, getattr(NtxStrategy, n).__get__(fake, NtxStrategy))
    return fake, order, sent, timers, drain


def test_no_stop_is_cancelled_while_another_replacement_is_still_queued():
    fake, order, sent, timers, drain = _fake(["A", "B", "C"])
    NtxStrategy._adopt_positions(fake)

    releases = [o for o in order if o[0] == "release"]
    assert len(releases) == 1, (
        f"on_start cancelled {len(releases)} stops before the event loop could send any "
        f"replacement: {order}"
    )
    assert "ntx-adoption" in timers, "the rest of the plan must be scheduled, not abandoned"

    # The loop gets control: A's replacement actually reaches the venue.
    drain()
    assert sent == ["A"]

    timers["ntx-adoption"](None)
    assert ("release", "B") in order
    assert order.index(("SENT", "A")) < order.index(("release", "B"))
    drain()
    timers["ntx-adoption"](None)
    drain()
    assert sent == ["A", "B", "C"]


def test_every_release_is_followed_by_its_own_replacement():
    """L-10's original contract, unchanged: cancel then immediately re-place, per
    instrument -- never cancel everything up front."""
    fake, order, _sent, timers, drain = _fake(["A", "B"])
    NtxStrategy._adopt_positions(fake)
    drain()
    timers["ntx-adoption"](None)
    drain()
    for inst in ("A", "B"):
        assert order.index(("release", inst)) < order.index(("queue", inst))


def test_the_timer_stops_when_the_plan_is_done():
    """A timer left running would re-enter adoption every tick for the life of
    the node."""
    fake, _order, _sent, timers, drain = _fake(["A", "B"])
    NtxStrategy._adopt_positions(fake)
    drain()
    timers["ntx-adoption"](None)
    drain()
    assert "ntx-adoption" not in timers


def test_a_single_position_needs_no_timer():
    fake, order, _sent, timers, _drain = _fake(["A"])
    NtxStrategy._adopt_positions(fake)
    assert ("release", "A") in order and ("queue", "A") in order
    assert "ntx-adoption" not in timers


def test_one_bad_release_does_not_strand_the_positions_already_adopted():
    """If B's release raises, A has already been adopted AND sent. Before the fix
    the exception left on_start half-done with A's stop queued and unsent."""
    fake, order, sent, timers, drain = _fake(["A", "B"])

    def boom(inst):
        order.append(("release", inst))
        if inst == "B":
            raise RuntimeError("venue timeout")
        return 95.0

    fake.venue_stop_hook = boom
    NtxStrategy._adopt_positions(fake)
    drain()
    assert sent == ["A"], "A must be protected before B is even attempted"
    with pytest.raises(RuntimeError):
        timers["ntx-adoption"](None)


def test_the_tick_is_short_enough_to_matter():
    """The exposure window is one tick per position; minutes would defeat it."""
    assert timedelta(seconds=NtxStrategy.ADOPT_TICK_SECS) <= timedelta(seconds=5)
