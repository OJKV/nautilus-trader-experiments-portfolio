"""F067: a position nautilus cannot reconcile disappears from everything we own.

Found in the 48-hour log sweep on 2026-09-27. nautilus's own exec engine, on the
demo soak:

    Position discrepancy detected for LINK-USDT-SWAP.OKX:
        cached_qty=0 (flat), venue_qty=5.69; querying for missing fills...
    Position discrepancy for LINK-USDT-SWAP.OKX persists but no missing fills found
    Position discrepancy for LINK-USDT-SWAP.OKX unresolved after 3 attempts
        (cached_qty=0, venue_qty=5.69); no further reconciliation attempts will be made

The account holds 5.69 LINK. The cache does not, so:

* it is absent from the state snapshot, so the dashboard cannot show it;
* `positions_unprotected` never counts it, because that metric is computed over
  positions the node knows about;
* the strategy has no state for it, so no exit, no trailing stop, nothing;
* its journal row was closed by F066 the day before, so no journal check sees it.

Every safety net we have is keyed on our own view of the world, and this is a
position that exists only in the venue's. nautilus says plainly that it will stop
trying -- so something else has to keep looking, or the position is simply gone
from view while remaining entirely real.

R53 already says the unprotected metric is defined over VENUE positions, never
internal state, "the cases it exists to catch are exactly the ones with no
state". This is that rule applied one level up: the *inventory* has to come from
the venue too.

It does not refuse to start. An unmanaged position is bad; taking every other
position down with it is worse, and the node cannot conjure the cache entry
nautilus declined to make. It alarms, loudly, every pass, until a human deals
with it.

Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy

LINK = "LINK-USDT-SWAP.OKX"
SOL = "SOL-USDT-SWAP.OKX"


def _fake(*, venue, cache_qty, hook=True):
    logged: list = []
    events: list = []

    def positions():
        return venue

    fake = SimpleNamespace(
        instruments=dict.fromkeys([SOL, LINK]),
        states={},
        journal=None,
        unmanaged_venue_positions=0,
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: logged.append(str(a[0]) if a else ""),
            debug=lambda *a: None,
        ),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"))),
        _position_qty=lambda inst: cache_qty.get(str(inst), 0.0),
        venue_position_hook=positions if hook else None,
    )
    NtxStrategy._check_venue_inventory.__get__(fake, NtxStrategy)()
    return fake, logged, events


def test_a_venue_position_the_cache_does_not_have_is_reported():
    fake, logged, events = _fake(venue={"LINK-USDT-SWAP": 5.69}, cache_qty={})
    assert ("unmanaged_venue_position", "critical") in events
    assert any("LINK" in line and "5.69" in line for line in logged), logged
    assert fake.unmanaged_venue_positions == 1


def test_a_position_the_node_manages_is_not_reported():
    _, logged, events = _fake(venue={"SOL-USDT-SWAP": 2.02}, cache_qty={SOL: 2.02})
    assert events == []
    assert logged == []


def test_only_instruments_we_trade_are_our_problem():
    """Another strategy's sub-account position is not this node's to alarm on."""
    _, _logged, events = _fake(venue={"DOGE-USDT-SWAP": 100.0}, cache_qty={})
    assert events == []


def test_a_short_counts_too():
    fake, _logged, events = _fake(venue={"LINK-USDT-SWAP": -5.69}, cache_qty={})
    assert ("unmanaged_venue_position", "critical") in events
    assert fake.unmanaged_venue_positions == 1


def test_a_venue_that_will_not_answer_is_not_silence():
    """R70: a failed read must not look like "nothing unmanaged"."""

    def boom():
        raise OSError("okx timeout")

    logged: list = []
    events: list = []
    fake = SimpleNamespace(
        instruments=dict.fromkeys([SOL, LINK]),
        states={},
        journal=None,
        unmanaged_venue_positions=0,
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: logged.append(str(a[0]) if a else ""),
            error=lambda *a: logged.append(str(a[0]) if a else ""),
            debug=lambda *a: None,
        ),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"))),
        _position_qty=lambda inst: 0.0,
        venue_position_hook=boom,
    )
    NtxStrategy._check_venue_inventory.__get__(fake, NtxStrategy)()
    assert ("venue_inventory_unreadable", "warning") in events


def test_no_hook_is_a_no_op():
    _, logged, events = _fake(venue={}, cache_qty={}, hook=False)
    assert events == [] and logged == []


def test_the_watchdog_runs_it():
    import inspect

    src = inspect.getsource(NtxStrategy._protection_watchdog)
    assert "_check_venue_inventory" in src, "a check nothing calls is not a check"
