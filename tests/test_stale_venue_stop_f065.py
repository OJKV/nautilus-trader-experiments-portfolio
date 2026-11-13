"""F065: a live venue stop is not proof that THIS position is protected.

Measured on the demo box, 2026-09-27. A SOL long opened 08:00 UTC at 124.11
wanting a stop at 119.374. OKX refused it:

    OKX error 1: You can only place 1 TP/SL order to close an entire position

`_already_protected` peeked at the venue, found a stop at 115.597, and concluded
"the position is protected, not naked; not closing". There *was* a stop -- but it
belonged to the SOL position from 2026-09-25, which had been flattened while the
node was down. Adoption closed that trade's journal row (`closed_while_offline`)
and never cancelled its venue stop: nautilus does not load OKX algo orders, so
neither the order cache nor the stale-stop sweep can see one.

Two consequences, both reachable on live:

1. The new trade ran at 1.06 x (124.11 - 115.597) = $9.02 of risk against a $5.00
   budget -- a silent 1.8x breach of `risk_equity_pct` (R5), invisible in every
   report because the journal records the stop we *wanted*.
2. It never converged. The watchdog reads live stops and said "unprotected"; the
   refusal handler read the venue and said "protected". Each fought the other
   every 20 minutes for five hours, logging an ERROR each pass, and the health
   check sat at `fail` the whole time -- an alarm that cannot clear is an alarm
   you learn to ignore (R70).

So a duplicate refusal is only proof of protection when the live stop is at the
price this trade actually needs. Anything else is a stale stop: cancel it and
place the right one, or close the position (R15).

Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy

DUP = "OKX error 1: You can only place 1 TP/SL order to close an entire position"
SOL = "SOL-USDT-SWAP.OKX"

WANTED = 119.37428571428572  # what the 2026-09-27 trade asked for
STALE = 115.59714285714287  # what the 2026-09-25 trade left behind


def _fake(*, peek, release=None, wanted=WANTED, reason=DUP):
    acted: list[tuple] = []
    events: list[tuple] = []
    state = SimpleNamespace(
        meta={"trade_id": "t-2", "covered_qty": 1.06},
        direction=None,
        stop_price=wanted,
        entry_price=124.11,
        base_units=1.06,
    )
    fake = SimpleNamespace(
        states={SOL: state},
        stop_orders={SOL: object()},
        stop_client_order_ids={"o-stop": SOL},
        _close_orders={},
        journal=None,
        unprotected_events=0,
        stops_refused=0,
        entries_refused=0,
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: acted.append(("error", a[0] if a else "")),
            debug=lambda *a: None,
        ),
        _exit=lambda inst, why: acted.append(("exit", str(inst), str(why))),
        _flatten=lambda inst: acted.append(("flatten", str(inst))),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"))),
        _jwrite=lambda *a, **k: None,
        _close_refused=lambda *a: None,
        _submit_stop=lambda inst, st, qty: acted.append(("submit_stop", str(inst), st.stop_price)),
        venue_stop_peek_hook=peek,
        venue_stop_hook=release,
    )
    for n in (
        "DUPLICATE_STOP_SIGNS",
        "STALE_STOP_TOLERANCE_PCT",
        "_handle_refused",
        "_already_protected",
        "_stop_price_matches",
        "_replace_stale_stop",
    ):
        attr = getattr(NtxStrategy, n, None)
        if attr is None:
            continue
        setattr(fake, n, attr.__get__(fake, NtxStrategy) if callable(attr) else attr)
    event = SimpleNamespace(client_order_id="o-stop", reason=reason)
    return fake, event, acted, events


def _refuse(fake, event):
    NtxStrategy._handle_refused.__get__(fake, NtxStrategy)(event, "rejected")


def test_a_stop_at_the_price_this_trade_wants_is_still_accepted():
    """The F063 behaviour must survive: a matching stop means protected."""
    fake, event, acted, _events = _fake(peek=lambda inst: WANTED)
    _refuse(fake, event)
    assert not [a for a in acted if a[0] in ("exit", "flatten")]
    assert fake.states[SOL].meta.get("venue_stop_price") == WANTED


def test_a_stale_stop_from_an_earlier_position_is_not_protection():
    """115.597 belonged to a trade that closed two days earlier."""
    cancelled: list[str] = []
    fake, event, acted, _events = _fake(
        peek=lambda inst: STALE,
        release=lambda inst: cancelled.append(str(inst)) or STALE,
    )
    _refuse(fake, event)
    assert cancelled == [SOL], f"the stale stop was left at the venue: {acted}"
    assert ("submit_stop", SOL, WANTED) in acted, (
        f"no stop was placed at the price this trade needs: {acted}"
    )


def test_a_stale_stop_is_never_silently_recorded_as_this_trades_protection():
    fake, event, _acted, _events = _fake(peek=lambda inst: STALE, release=lambda inst: STALE)
    _refuse(fake, event)
    assert fake.states[SOL].meta.get("venue_stop_price") != STALE, (
        "recording a stale trigger as protection hides a risk breach from every report"
    )


def test_a_stale_stop_we_cannot_cancel_closes_the_position():
    """No release hook: we cannot make it right, so R15 wins."""
    fake, event, acted, _events = _fake(peek=lambda inst: STALE, release=None)
    _refuse(fake, event)
    assert [a for a in acted if a[0] in ("exit", "flatten")], (
        "a position guarded only by someone else's stop is not protected (R15)"
    )


def test_a_refused_cancel_closes_the_position():
    def boom(inst):
        raise RuntimeError("cancel rejected")

    fake, event, acted, _events = _fake(peek=lambda inst: STALE, release=boom)
    _refuse(fake, event)
    assert [a for a in acted if a[0] in ("exit", "flatten")]


def test_the_stale_stop_is_reported_as_critical():
    fake, event, _acted, events = _fake(peek=lambda inst: STALE, release=lambda inst: STALE)
    _refuse(fake, event)
    assert ("stale_stop_replaced", "critical") in events, (
        f"a 1.8x risk breach must not be logged as routine: {events}"
    )


def test_tick_rounding_does_not_count_as_stale():
    """OKX rounds the trigger to the instrument tick; that is not a stale stop."""
    fake, event, acted, _events = _fake(peek=lambda inst: WANTED + 0.0001)
    _refuse(fake, event)
    assert not [a for a in acted if a[0] in ("exit", "flatten", "submit_stop")]
