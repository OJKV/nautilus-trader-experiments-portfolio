"""F073 / N3-01: R74 held on one close path out of two.

F069 was fixed in `_flatten`. The normal exit -- Donchian, trailing stop, KILL,
STOP_REFUSED and the watchdog's unfinished-close retry -- goes `_exit ->
_submit_close`, which sized a market order from the cache (or, with an empty
cache, from the entry's `filled_qty`) and sent it with no venue read. A phantom
in the cache therefore still sent F069's trade, just through a different door:
adoption takes a phantom with a same-side journal row, OKX refuses its stop
(there is no position), STOP_REFUSED exits, and the "close" opens a position.

Reduce-only did not stop F069 on this account, so it is not the protection.
The venue is: an exit closes what the venue says is there, on the side we think
it is, and nothing when the venue says flat or cannot be read.

Also N3-02: an empty cache used to confirm the close (journal closed, stop
cancelled) on cache emptiness alone -- R72.

Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy
from ntx.strategies.exits import Direction, ExitReason, PositionState

LINK = "LINK-USDT-SWAP.OKX"


def _quiet(*_a) -> None:
    return None


def _fake(*, cached, venue, direction=Direction.SHORT, filled_qty=0.6, hook=True):
    submitted: list = []
    confirmed: list = []
    events: list = []

    def positions():
        if venue is None:
            raise OSError("okx timeout")
        return venue

    def market(**kw):
        return SimpleNamespace(client_order_id=f"C-{len(submitted)}", **kw)

    fake = SimpleNamespace(
        id="S003",
        states={},
        stop_orders={},
        _close_orders={},
        exit_counts={},
        journal=None,
        instruments={LINK: SimpleNamespace(make_qty=lambda q: q)},
        order_factory=SimpleNamespace(market=market),
        submit_order=lambda o, params=None: submitted.append(o),
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: (
                [SimpleNamespace(quantity=abs(cached), signed_qty=cached)] if cached else []
            )
        ),
        log=SimpleNamespace(info=_quiet, warning=_quiet, error=_quiet, debug=_quiet),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"))),
        _venue_writes_allowed=lambda why: True,
        _close_confirmed=lambda inst, state: confirmed.append(inst),
        venue_position_hook=positions if hook else None,
    )
    for name in ("_exit", "_submit_close", "_position_qty", "_venue_close_qty"):
        attr = getattr(NtxStrategy, name, None)
        if attr is not None:
            setattr(fake, name, attr.__get__(fake, NtxStrategy))
    fake.states[LINK] = PositionState(
        direction=direction, entry_price=10.0, stop_price=11.0, margin_usd=2.0,
        meta={"filled_qty": filled_qty, "trade_id": "t-1"},
    )  # fmt: skip
    return fake, submitted, confirmed, events


def test_a_phantom_short_with_a_matching_journal_row_sends_nothing():
    """The report's reproduction: cache SHORT 0.6, venue nothing, row SHORT."""
    fake, submitted, _confirmed, _events = _fake(cached=-0.6, venue={})
    fake._exit(LINK, ExitReason.STOP_REFUSED)
    assert submitted == [], "a close on a flat venue OPENS a position (F069)"


def test_a_flat_venue_finalises_the_exit_instead():
    """Nothing to close is a finished exit -- because the VENUE says so (R49)."""
    fake, _submitted, confirmed, _events = _fake(cached=-0.6, venue={})
    fake._exit(LINK, ExitReason.STOP_REFUSED)
    assert confirmed == [LINK]


def test_the_venue_holding_the_other_side_sends_nothing_and_shouts():
    fake, submitted, confirmed, events = _fake(cached=-0.6, venue={"LINK-USDT-SWAP": 0.6})
    fake._exit(LINK, ExitReason.KILL_SWITCH)
    assert submitted == [] and confirmed == []
    assert ("phantom_flatten_refused", "critical") in events
    assert LINK in fake.states, "the position is not forgotten on a disagreement"


def test_an_unreadable_venue_sends_nothing_and_forgets_nothing():
    fake, submitted, confirmed, events = _fake(cached=-0.6, venue=None)
    fake._exit(LINK, ExitReason.KILL_SWITCH)
    assert submitted == [] and confirmed == []
    assert ("phantom_flatten_refused", "critical") in events


def test_a_real_position_is_closed_for_exactly_what_the_venue_holds():
    fake, submitted, _confirmed, _events = _fake(cached=-1.2, venue={"LINK-USDT-SWAP": -0.6})
    fake._exit(LINK, ExitReason.KILL_SWITCH)
    assert len(submitted) == 1
    assert submitted[0].quantity == 0.6, "closing 1.2 against a real 0.6 opens 0.6 long"
    assert submitted[0].order_side.name == "BUY"


def test_an_empty_cache_closes_the_venue_position_not_the_entry_size():
    """N3-02: an empty cache may be "not reconciled yet" (R72). The old code
    either closed the entry's filled_qty blind, or -- with no filled_qty --
    confirmed the close on cache emptiness alone."""
    fake, submitted, confirmed, _events = _fake(
        cached=0.0, venue={"LINK-USDT-SWAP": -0.4}, filled_qty=0.6
    )
    fake._exit(LINK, ExitReason.KILL_SWITCH)
    assert confirmed == []
    assert [o.quantity for o in submitted] == [0.4]


def test_without_a_venue_hook_the_backtest_path_is_unchanged():
    fake, submitted, _confirmed, _events = _fake(cached=-0.6, venue={}, hook=False)
    fake._exit(LINK, ExitReason.KILL_SWITCH)
    assert [o.quantity for o in submitted] == [0.6]
