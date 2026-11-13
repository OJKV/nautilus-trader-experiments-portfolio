"""A stop refused because one already exists means PROTECTED, not naked.

Measured on the box during the R44 drill, 2026-09-24 11:14:23 UTC:

    protective stop cancelled externally -- re-attaching
    stop_refused  critical  {"reason":"OKX error 1: You can only place 1 TP/SL
                             order to close an entire position"}

OKX allows exactly one close-position stop per position (F036). So that refusal
is the venue saying "this position already has its stop" -- the position is
protected. The handler read every stop refusal as "the position is naked" and
closed it (R15, correct in general), which here exits a healthy, fully protected
trade for no reason. On live that is a mystery exit and lost edge, and it is
reachable any time a re-attach races the venue's own bookkeeping.

The conservative default is kept for every other refusal: if we cannot prove a
stop is live at the venue, the position still gets closed.

Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy

DUP = "OKX error 1: You can only place 1 TP/SL order to close an entire position"
SOL = "SOL-USDT-SWAP.OKX"


def _fake(*, peek, reason=DUP):
    acted: list[tuple] = []
    events: list[tuple] = []
    state = SimpleNamespace(meta={"trade_id": "t-1"}, direction=None)
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
        venue_stop_peek_hook=peek,
    )
    fake.DUPLICATE_STOP_SIGNS = NtxStrategy.DUPLICATE_STOP_SIGNS
    for n in ("_handle_refused", "_already_protected"):
        if hasattr(NtxStrategy, n):
            setattr(fake, n, getattr(NtxStrategy, n).__get__(fake, NtxStrategy))
    event = SimpleNamespace(client_order_id="o-stop", reason=reason)
    return fake, event, acted, events


def _refuse(fake, event):
    NtxStrategy._handle_refused.__get__(fake, NtxStrategy)(event, "rejected")


def test_a_duplicate_stop_refusal_does_not_close_a_protected_position():
    fake, event, acted, events = _fake(peek=lambda inst: 112.519)
    _refuse(fake, event)
    assert not [a for a in acted if a[0] in ("exit", "flatten")], (
        f"closed a position the venue says is already protected: {acted}"
    )
    assert ("stop_refused", "critical") not in events


def test_the_existing_venue_stop_is_recorded_as_protection():
    fake, event, _acted, _events = _fake(peek=lambda inst: 112.519)
    _refuse(fake, event)
    assert fake.states[SOL].meta.get("venue_stop_price") == 112.519


def test_any_other_refusal_still_closes_the_position():
    fake, event, acted, events = _fake(peek=lambda inst: None, reason="insufficient margin")
    _refuse(fake, event)
    assert [a for a in acted if a[0] in ("exit", "flatten")], (
        "a naked position must be closed (R15)"
    )
    assert ("stop_refused", "critical") in events


def test_a_duplicate_refusal_we_cannot_verify_still_closes():
    """If the peek fails we do not know the position is protected. Close it."""

    def boom(inst):
        raise RuntimeError("venue timeout")

    fake, event, acted, _events = _fake(peek=boom)
    _refuse(fake, event)
    assert [a for a in acted if a[0] in ("exit", "flatten")]


def test_a_duplicate_refusal_with_no_stop_at_the_venue_still_closes():
    fake, event, acted, _events = _fake(peek=lambda inst: None)
    _refuse(fake, event)
    assert [a for a in acted if a[0] in ("exit", "flatten")]
