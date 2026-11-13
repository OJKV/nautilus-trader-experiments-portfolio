"""An unmanaged position with a stop is not the same emergency as a naked one.

2026-10-05, after a reboot. The live account held BNB 3.0 @ 789.7 with its venue
stop live at 773.57 -- exactly the stop the journal asked for, exactly the $0.48
of risk the sizing intended. nautilus could not rebuild the position because its
opening fill was older than the 60-minute reconciliation lookback, so the node
had no state for it, and `_check_venue_inventory` reported:

    WATCHDOG: the venue holds 3.0 BNB-USDT-SWAP that this node does not manage
    -- no state, no exit, no trailing stop. Reconciliation never took it.

True, and worth saying. But it went out as `critical`, identical in every way to
the alarm for a position with nothing guarding it at all. Those demand opposite
responses:

* **unmanaged and naked** -- money is at risk right now, act immediately;
* **unmanaged but protected** -- the loss is capped at the number we chose; what
  is lost is the *upside*, because the Donchian exit and the trailing stop cannot
  run. On S003 that matters: both live winners so far exited on the trailing
  stop. It needs attention this week, not this minute.

Collapsing the two is how a channel earns a mute (R75), and this project has
already paid that bill once.
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy

BNB = "BNB-USDT-SWAP.OKX"


def _fake(*, venue, stop, instruments=(BNB,)):
    events: list = []
    logged: list = []

    def peek(inst):
        if isinstance(stop, Exception):
            raise stop
        return stop

    fake = SimpleNamespace(
        instruments=dict.fromkeys(instruments),
        states={},
        journal=None,
        unmanaged_venue_positions=0,
        _phantom_instruments=set(),
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: logged.append(("warn", str(a[0]) if a else "")),
            error=lambda *a: logged.append(("error", str(a[0]) if a else "")),
            debug=lambda *a: None,
        ),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"), kw.get("reason", ""))),
        _position_qty=lambda inst: 0.0,
        venue_position_hook=lambda: venue,
        venue_stop_peek_hook=peek,
    )
    NtxStrategy._check_venue_inventory(fake)
    return fake, events, logged


def test_unmanaged_and_naked_is_critical():
    _f, events, _l = _fake(venue={"BNB-USDT-SWAP": 3.0}, stop=None)
    kinds = [(k, s) for k, s, _r in events]
    assert ("unmanaged_venue_position", "critical") in kinds


def test_unmanaged_but_protected_is_a_warning_not_a_critical():
    """The exact 2026-10-05 BNB case."""
    _f, events, _l = _fake(venue={"BNB-USDT-SWAP": 3.0}, stop=773.57)
    kinds = [(k, s) for k, s, _r in events]
    assert ("unmanaged_venue_position", "warning") in kinds
    assert ("unmanaged_venue_position", "critical") not in kinds


def test_the_protected_message_says_what_is_actually_lost():
    """Not "your money is at risk" -- the exits are."""
    _f, events, _l = _fake(venue={"BNB-USDT-SWAP": 3.0}, stop=773.57)
    reason = " ".join(r for _k, _s, r in events)
    assert "773.57" in reason
    assert "exit" in reason.lower()


def test_a_stop_we_cannot_check_is_treated_as_naked():
    """R70: not knowing whether it is protected is not knowing it is safe."""
    _f, events, _l = _fake(venue={"BNB-USDT-SWAP": 3.0}, stop=OSError("okx timeout"))
    kinds = [(k, s) for k, s, _r in events]
    assert ("unmanaged_venue_position", "critical") in kinds


def test_no_peek_hook_still_reports_critical():
    _f, events, _l = _fake(venue={"BNB-USDT-SWAP": 3.0}, stop=None)
    _f.venue_stop_peek_hook = None
    assert [k for k, _s, _r in events]


def test_it_is_still_counted_either_way():
    f, _e, _l = _fake(venue={"BNB-USDT-SWAP": 3.0}, stop=773.57)
    assert f.unmanaged_venue_positions == 1


def test_a_venue_with_nothing_on_it_reports_nothing():
    _f, events, logged = _fake(venue={}, stop=None)
    assert events == []
    assert logged == []
