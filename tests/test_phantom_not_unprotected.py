"""A phantom in the cache is not an unprotected position.

After the F069 guard shipped, the live deploy's final gate failed:

    UNHEALTHY: 2 position(s) without a venue stop (R15)

The venue was flat — verified in the same minute. The two "positions" were the
phantoms nautilus's cache had reconstructed and that the new guard had just
correctly refused to trade on. So the node was behaving exactly as designed and
its own health check called it broken.

R53 says the unprotected metric is defined over VENUE positions and LIVE stops,
"never over internal state or dict membership". Half of it obeyed that and half
of it counted `cache.positions_open()`, which is internal state wearing a venue
costume.

Both conditions are still worth shouting about, and they are different things:

* venue has it, cache does not  -> unmanaged: no exit, no stop, invisible (F067)
* cache has it, venue does not  -> phantom: nothing real, but it distorts every
  count derived from the cache, and a close sent against it opens a position

Neither is "a position without a venue stop", which is the one metric that must
read 0 and that a human is expected to act on immediately.
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy

SOL = "SOL-USDT-SWAP.OKX"
LINK = "LINK-USDT-SWAP.OKX"


def _fake(*, cache_qty, venue, phantoms=None):
    events: list = []
    fake = SimpleNamespace(
        instruments=dict.fromkeys([SOL, LINK]),
        instrument_ids=[SOL, LINK],
        states={},
        stop_orders={},
        journal=None,
        unmanaged_venue_positions=0,
        _phantom_instruments=set(phantoms or ()),
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: None,
            debug=lambda *a: None,
        ),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"))),
        _position_qty=lambda inst: cache_qty.get(str(inst), 0.0),
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: (
                [object()] if cache_qty.get(str(instrument_id)) else []
            )
        ),
        venue_position_hook=(lambda: venue) if venue is not None else None,
    )
    return fake, events


def test_a_phantom_is_not_counted_as_an_unprotected_position():
    fake, _events = _fake(cache_qty={SOL: 0.09}, venue={}, phantoms={SOL})
    assert NtxStrategy.positions_unprotected_count(fake) == 0


def test_a_real_position_with_no_stop_is_still_counted():
    """The metric must not be softened into uselessness."""
    fake, _events = _fake(cache_qty={SOL: 0.09}, venue={"SOL-USDT-SWAP": 0.09})
    assert NtxStrategy.positions_unprotected_count(fake) == 1


def test_the_inventory_check_records_phantoms():
    fake, events = _fake(cache_qty={SOL: 0.09}, venue={})
    NtxStrategy._check_venue_inventory(fake)
    assert SOL in fake._phantom_instruments
    assert ("phantom_cache_position", "critical") in events


def test_a_phantom_that_becomes_real_is_forgotten():
    """The set must clear, or one bad minute mutes the metric forever."""
    fake, _events = _fake(cache_qty={SOL: 0.09}, venue={"SOL-USDT-SWAP": 0.09}, phantoms={SOL})
    NtxStrategy._check_venue_inventory(fake)
    assert fake._phantom_instruments == set()


def test_an_unreadable_venue_does_not_invent_phantoms():
    """R70: a failed read must not silence the unprotected metric."""

    def boom():
        raise OSError("okx timeout")

    fake, _events = _fake(cache_qty={SOL: 0.09}, venue={}, phantoms={SOL})
    fake.venue_position_hook = boom
    NtxStrategy._check_venue_inventory(fake)
    assert fake._phantom_instruments == {SOL}, "an unread venue changes nothing either way"


def test_unmanaged_and_phantom_are_different_alarms():
    fake, events = _fake(cache_qty={}, venue={"SOL-USDT-SWAP": 0.09})
    NtxStrategy._check_venue_inventory(fake)
    assert ("unmanaged_venue_position", "critical") in events
    assert ("phantom_cache_position", "critical") not in events


def test_the_inventory_is_taken_at_start_not_only_on_the_watchdog():
    """The phantom set is empty until something fills it.

    It was filled only by the 20-minute watchdog, so for 20 minutes after every
    restart the unprotected metric counted phantoms again -- and a deploy
    verifies inside that window. Two live deploys in a row failed their final
    gate on an account that was flat.
    """
    import inspect

    src = inspect.getsource(NtxStrategy.on_start)
    assert "_check_venue_inventory" in src, (
        "the metric is wrong until the first inventory; take one at start"
    )
    assert src.index("_adopt_positions") < src.index("_check_venue_inventory"), (
        "adoption first: it is what settles which positions we own"
    )


def test_the_orphan_repair_leaves_phantoms_alone():
    """A phantom is not an orphan, and calling it one cost 854 alerts.

    `_repair_orphan_positions` existed to close a VENUE position with no state.
    A phantom is the opposite: no venue position at all. Treating it as an orphan
    had `_flatten` refuse the close on every watchdog pass and fire two criticals
    each time, for three days (F070). The phantom has its own alarm already.
    """
    from types import SimpleNamespace

    acted: list = []
    fake = SimpleNamespace(
        instruments={SOL: None},
        states={},
        orphans_closed=0,
        journal=None,
        _phantom_instruments={SOL},
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: acted.append("log"),
            debug=lambda *a: None,
        ),
        _jevent=lambda *a, **k: acted.append("event"),
        _flatten=lambda inst: acted.append("flatten"),
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: [object()],
            orders_open=lambda instrument_id=None: [],
        ),
    )
    NtxStrategy._repair_orphan_positions(fake)
    assert acted == [], f"a phantom must not be treated as an orphan: {acted}"
    assert fake.orphans_closed == 0


def test_a_real_orphan_is_still_closed():
    from types import SimpleNamespace

    acted: list = []
    fake = SimpleNamespace(
        instruments={SOL: None},
        states={},
        orphans_closed=0,
        journal=None,
        _phantom_instruments=set(),
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: None,
            debug=lambda *a: None,
        ),
        _jevent=lambda *a, **k: None,
        _flatten=lambda inst: acted.append("flatten"),
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: [object()],
            orders_open=lambda instrument_id=None: [],
        ),
    )
    NtxStrategy._repair_orphan_positions(fake)
    assert acted == ["flatten"]


# ---- R75: a phantom that has not moved is one journal event, not one per pass -- #


def test_a_standing_phantom_is_journalled_once_not_every_watchdog_pass():
    """Live 2026-10-07/08: the BNB phantom was a fresh CRITICAL every 20 min (72 a
    day), holding the health check red for days over one unchanged fact."""
    fake, events = _fake(cache_qty={SOL: 0.09}, venue={})
    for _ in range(3):
        NtxStrategy._check_venue_inventory(fake)
    assert events.count(("phantom_cache_position", "critical")) == 1


def test_a_phantom_that_clears_and_returns_is_news_again():
    fake, events = _fake(cache_qty={SOL: 0.09}, venue={})
    NtxStrategy._check_venue_inventory(fake)
    fake._position_qty = lambda inst: 0.0  # cleared
    NtxStrategy._check_venue_inventory(fake)
    fake._position_qty = lambda inst: 0.09 if str(inst) == SOL else 0.0  # back
    NtxStrategy._check_venue_inventory(fake)
    assert events.count(("phantom_cache_position", "critical")) == 2


def test_a_standing_protected_unmanaged_position_is_journalled_once():
    fake, events = _fake(cache_qty={}, venue={"SOL-USDT-SWAP": 0.5})
    fake.venue_stop_peek_hook = lambda inst: 120.0
    for _ in range(3):
        NtxStrategy._check_venue_inventory(fake)
    assert events.count(("unmanaged_venue_position", "warning")) == 1
