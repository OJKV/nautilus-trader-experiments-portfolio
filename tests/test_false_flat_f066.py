"""F066: "the cache has no position" is not "the venue is flat".

Measured on the demo box. 2026-09-26 16:13:32, four seconds after the exec
engine began reconciling, `on_start` ran and adoption concluded:

    ADOPTION close_journal SOL-USDT-SWAP.OKX: venue flat: closed while offline
    ADOPTION close_journal LINK-USDT-SWAP.OKX: venue flat: closed while offline

SOL was not flat. The 2026-09-25 position -- 0.96 @ 120.79 -- was still open at
OKX, still carrying its own stop at 115.597. Adoption had simply read the cache
before reconciliation finished filling it.

The journal row was closed, so the instrument looked free, so the 2026-09-27
08:00 signal opened a *second* SOL long of 1.06. OKX runs `net_mode`, so the two
became one position of 2.02 -- and the venue's own arithmetic proves the parent:

    (0.96 x 120.79 + 1.06 x 124.11) / 2.02 = 122.532
    ExecEngine: Position avg_px verified: internal=122.5321782178, venue=122.5321782178

The consequences compound. The journal says 1.06 and the venue holds 2.02, so
every risk number derived from the journal is half of the truth. The inherited
stop at 115.597 covers all 2.02, which is $17.19 of risk against a $5.00 budget.
And the new position's own stop was refused -- "You can only place 1 TP/SL order
to close an entire position" -- because the 25th's stop was not an orphan at all:
it was the live stop of a live position (this is the correction to F065).

L-09 already established the principle for stops: "the venue did not answer" is
NOT "there is no stop". Nobody applied it to positions, where it matters more --
a stop wrongly believed absent gets replaced, a position wrongly believed closed
gets opened again on top of itself.

Live restarts on every deploy, and the new second door makes deploying over an
open position routine, so this is a live-money defect and not a demo curiosity.

Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.live.reconcile import AdoptAction, VenuePosition, adoption_plan
from ntx.strategies.base import NtxStrategy

SOL = "SOL-USDT-SWAP.OKX"
LINK = "LINK-USDT-SWAP.OKX"


def _row(inst, qty=1.06, side="LONG", trade_id="t-2"):
    return {
        "instrument_id": inst,
        "side": side,
        "qty": qty,
        "entry_price": 124.11,
        "stop_price": 119.374,
        "trade_id": trade_id,
    }


# ---- the plan must say what it does not know ------------------------------ #


def test_a_quantity_mismatch_is_not_agreement():
    """The venue held 2.02 while the journal said 1.06, and it read as "agree"."""
    plan = adoption_plan([VenuePosition(SOL, 2.02, 122.532, True)], [_row(SOL, qty=1.06)], {})
    assert len(plan) == 1
    assert plan[0].action is AdoptAction.ADOPT, "the position must still be managed"
    assert "agree" not in plan[0].reason, f"a 2x size difference is not agreement: {plan[0].reason}"
    assert plan[0].quantity == 2.02, "the venue is the truth about size (R27)"


def test_a_matching_quantity_still_agrees():
    plan = adoption_plan([VenuePosition(SOL, 1.06, 124.11, True)], [_row(SOL, qty=1.06)], {})
    assert plan[0].reason == "journal + venue agree"


def test_a_rounding_difference_is_not_a_mismatch():
    plan = adoption_plan(
        [VenuePosition(SOL, 1.06 + 1e-12, 124.11, True)], [_row(SOL, qty=1.06)], {}
    )
    assert plan[0].reason == "journal + venue agree"


# ---- a journal row is only closed when the VENUE says flat ---------------- #


def _fake(*, cache_positions, venue_positions, writes=True):
    acted: list = []
    events: list = []
    inst_ids = {SOL: SOL, LINK: LINK}
    fake = SimpleNamespace(
        instruments=inst_ids,
        states={},
        stop_orders={},
        stop_client_order_ids={},
        atr={},
        exit_cfg=SimpleNamespace(tsl_enabled=False),
        config=SimpleNamespace(trading_enabled=writes, max_leverage=3.0),
        journal=SimpleNamespace(
            open_trades=lambda: [_row(SOL)],
            event=lambda *a, **k: events.append((a[0] if a else k.get("kind"), k.get("severity"))),
            trade_state=lambda tid: "open",
            transition=lambda tid, st, **k: acted.append(("transition", tid, st)),
        ),
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: None,
            debug=lambda *a: None,
        ),
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: cache_positions.get(str(instrument_id), []),
            orders_open=lambda instrument_id=None: [],
        ),
        venue_stop_peek_hook=lambda inst: None,
        venue_stop_hook=None,
        venue_stop_reap_hook=None,
        venue_position_hook=lambda: venue_positions,
        _venue_blocked=set(),
        _sleep=lambda _s: None,
        VENUE_FLAT_ATTEMPTS=2,
        VENUE_FLAT_WAIT_S=0.0,
        _submit_stop=lambda inst, state, qty: acted.append(("place", str(inst))),
        _flatten=lambda inst: acted.append(("flatten", str(inst))),
        _stop_attached_at_entry=lambda: False,
        id="S003",
        _adopt_queue=[],
        _adopt_timer_on=False,
        ADOPT_TICK_SECS=NtxStrategy.ADOPT_TICK_SECS,
    )
    for n in (
        "_adopt",
        "_jwrite",
        "_jevent",
        "_drain_adoptions",
        "_schedule_adoption_timer",
        "_cancel_adoption_timer",
        "_reap_orphan_stops",
        "_confirm_venue_flat",
        "_venue_checked",
    ):
        attr = getattr(NtxStrategy, n, None)
        if attr is not None:
            setattr(fake, n, attr.__get__(fake, NtxStrategy))
    return fake, acted, events


def test_an_empty_cache_does_not_close_a_journal_row_the_venue_still_holds():
    """The exact 2026-09-26 16:13 sequence.

    The first fix raised here and the node exited. F068 replaced that with
    retry-then-degrade -- the row still must not be closed, but taking the whole
    node down to say so cost 65 minutes of soak on 2026-09-28.
    """
    fake, acted, _events = _fake(cache_positions={}, venue_positions={SOL: 0.96})
    NtxStrategy._adopt_positions(fake)
    assert not [a for a in acted if a[0] == "transition"], (
        "a position the venue still holds must never be journalled as closed"
    )
    assert SOL in fake._venue_blocked, "and the instrument must not accept a new entry"


def test_a_genuinely_flat_venue_still_closes_the_row():
    fake, acted, _events = _fake(cache_positions={}, venue_positions={})
    NtxStrategy._adopt_positions(fake)
    assert ("transition", "t-2", "closed") in acted


def test_a_venue_that_will_not_answer_does_not_close_the_row():
    """R70/L-09: "I could not tell" is not "there is nothing there"."""

    def boom():
        raise OSError("okx timeout")

    fake, acted, _events = _fake(cache_positions={}, venue_positions={})
    fake.venue_position_hook = boom
    NtxStrategy._adopt_positions(fake)
    assert not [a for a in acted if a[0] == "transition"]
    assert SOL in fake._venue_blocked


def test_the_false_flat_is_reported_as_critical():
    fake, _acted, events = _fake(cache_positions={}, venue_positions={SOL: 0.96})
    NtxStrategy._adopt_positions(fake)
    assert ("adoption_false_flat", "critical") in events


# ---- N3-01: adoption must not take over a position the venue does not hold -- #


def _cached(qty):
    return [SimpleNamespace(signed_qty=qty, quantity=abs(qty), avg_px_open=124.11)]


def test_a_phantom_with_a_same_side_journal_row_is_not_adopted():
    """Cache SHORT 0.6, venue flat, journal SHORT: the old plan ADOPTED it, OKX
    refused its stop, and STOP_REFUSED sent F069's trade (F073)."""
    fake, acted, events = _fake(cache_positions={SOL: _cached(1.06)}, venue_positions={})
    fake._drain_adoptions = lambda *_a: None
    NtxStrategy._adopt_positions(fake)
    assert not fake._adopt_queue, "a position the venue does not hold must not be adopted"
    assert not [a for a in acted if a[0] in ("place", "flatten")], acted
    assert ("adoption_phantom_skipped", "critical") in events


def test_adoption_takes_the_venue_size_over_the_cache_size():
    fake, _acted, _events = _fake(cache_positions={SOL: _cached(2.02)}, venue_positions={SOL: 1.06})
    fake._drain_adoptions = lambda *_a: None
    NtxStrategy._adopt_positions(fake)
    assert [a.quantity for _i, a, _s in fake._adopt_queue] == [1.06], "the venue is the truth (R27)"
