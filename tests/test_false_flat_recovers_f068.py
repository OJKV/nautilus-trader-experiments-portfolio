"""F068: the F066 guard was right, and it still took the soak down for an hour.

2026-09-28 08:35. The demo node restarted while the venue held SOL 2.02 and the
cache reported flat -- exactly the condition F066 exists to catch, and catching
it was correct: journalling that position as closed is what produced the double
entry in the first place.

But the guard raised, the node exited, systemd restarted it, the same race
resolved the same way ten times in a row, `StartLimitBurst` tripped, and
ntx-soak sat `failed` for 65 minutes. A safety check that converts a transient
disagreement into a permanent outage is not a safety check; it is the failure
mode it was written to prevent, wearing better clothes.

Two things were wrong:

1. **It did not wait.** Reconciliation is asynchronous and takes a few seconds:
   measured 4 s on the restart that broke (too early) and 6 s on the one that
   worked. Re-reading after a moment is the entire fix for the common case.
2. **It refused everything to protect one instrument.** The danger is closing a
   journal row for a position that is still open, which frees the instrument for
   a second entry on top of the first. Not closing that one row, and refusing to
   enter that one instrument, prevents exactly that -- without taking the other
   nine down, and without leaving every open position unmanaged.

So: retry, then degrade. Never die.

Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy

SOL = "SOL-USDT-SWAP.OKX"
LINK = "LINK-USDT-SWAP.OKX"


def _fake(*, answers, sleeps=None):
    """`answers` is a list of venue replies, one per attempt."""
    seen = iter(answers)
    events: list = []
    logged: list = []
    fake = SimpleNamespace(
        instruments=dict.fromkeys([SOL, LINK]),
        journal=None,
        _venue_blocked=set(),
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: logged.append(str(a[0]) if a else ""),
            error=lambda *a: logged.append(str(a[0]) if a else ""),
            debug=lambda *a: None,
        ),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"))),
        venue_position_hook=lambda: next(seen),
        _sleep=lambda s: (sleeps if sleeps is not None else []).append(s),
        VENUE_FLAT_ATTEMPTS=NtxStrategy.VENUE_FLAT_ATTEMPTS,
        VENUE_FLAT_WAIT_S=0.0,
    )
    return fake, events, logged


def _confirm(fake, instruments):
    return NtxStrategy._confirm_venue_flat.__get__(fake, NtxStrategy)(instruments)


def test_a_race_that_clears_on_the_second_look_is_not_an_error():
    """4 seconds early on one restart, fine 6 seconds later on the next."""
    fake, events, _logged = _fake(answers=[{"SOL-USDT-SWAP": 2.02}, {}])
    keep = _confirm(fake, [SOL])
    assert keep == [SOL], "the row may be closed once the venue confirms flat"
    assert fake._venue_blocked == set()
    assert not [e for e in events if e[1] == "critical"]


def test_it_never_raises_when_the_venue_keeps_disagreeing():
    fake, _events, _logged = _fake(answers=[{"SOL-USDT-SWAP": 2.02}] * 12)
    keep = _confirm(fake, [SOL])  # must not raise
    assert keep == [], "the journal row must NOT be closed"


def test_the_disputed_instrument_is_blocked_from_new_entries():
    """Not closing the row is not enough: nothing else stops a second entry."""
    fake, _events, _logged = _fake(answers=[{"SOL-USDT-SWAP": 2.02}] * 12)
    _confirm(fake, [SOL])
    assert SOL in fake._venue_blocked


def test_the_other_instruments_are_untouched():
    """One disputed position must not take the other nine down."""
    fake, _events, _logged = _fake(answers=[{"SOL-USDT-SWAP": 2.02}] * 12)
    keep = _confirm(fake, [SOL, LINK])
    assert keep == [LINK]
    assert fake._venue_blocked == {SOL}


def test_it_is_reported_as_critical():
    fake, events, _logged = _fake(answers=[{"SOL-USDT-SWAP": 2.02}] * 12)
    _confirm(fake, [SOL])
    assert ("adoption_false_flat", "critical") in events


def test_a_venue_that_will_not_answer_blocks_rather_than_dies():
    def boom():
        raise OSError("okx timeout")

    fake, _events, _logged = _fake(answers=[])
    fake.venue_position_hook = boom
    keep = _confirm(fake, [SOL])
    assert keep == []
    assert SOL in fake._venue_blocked


def test_a_blocked_instrument_refuses_an_entry():
    import inspect

    src = inspect.getsource(NtxStrategy._enter)
    assert "_venue_blocked" in src, "blocking is only real if the entry path checks it"
