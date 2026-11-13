"""N2-04: the watchdog must REPAIR an orphan position, not just report it.

R15 says "attach at entry; watchdog repairs". The repair loop only ever looked at
`self.states`:

    held = [i for i in self.states if self.cache.positions_open(instrument_id=i)]

so a VENUE position with no state -- a late fill after an entry was cancelled or
rejected, or a KILL racing an unfilled entry -- was counted by
`positions_unprotected_count` (which is correctly defined over venue positions,
B-03) and pushed as a critical alert, and then nothing happened. Ever. Every exit
path in the strategy is keyed on `states`, so that position has no Donchian exit,
no trailing stop, no reversal and no stop: it sits there, leveraged, until a human
notices.

Closing it is the repair. Adopting would mean inventing an entry price and a stop
for a position we cannot explain; a confirmed close is deterministic (R49) and the
account is ours alone (R51).

Also pinned here: `_risk_check()` sat above the watchdog's docstring, unwrapped. A
raise there skipped the stale-stop sweep, the unfinished-close retries, the data-
stall detector and the re-attach loop -- the entire point of the pass.

Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy

# Real ids: the watchdog builds a BarType per instrument for the stall detector.
A = "SOL-USDT-SWAP.OKX"
B = "TRX-USDT-SWAP.OKX"


def _watchdog_fake(*, positions: dict, states: dict, open_orders=(), risk_raises=False):
    events: list[tuple] = []
    closed: list[str] = []

    fake = SimpleNamespace(
        instruments={A: SimpleNamespace(multiplier=1.0), B: SimpleNamespace(multiplier=1.0)},
        states=states,
        stop_orders={},
        stop_client_order_ids={},
        journal=None,
        config=SimpleNamespace(
            bar_spec="4-HOUR-LAST-EXTERNAL", trading_enabled=True, watchdog_minutes=20
        ),
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: None,
            debug=lambda *a: None,
        ),
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: positions.get(instrument_id, []),
            orders_open=lambda instrument_id=None: list(open_orders),
        ),
        clock=SimpleNamespace(timestamp_ns=lambda: 0),
        _awaiting_since={},
        _last_live_bar_ns={},
        _live_bars=0,
        positions_unprotected=0,
        unprotected_events=0,
        orphans_closed=0,
        _sweep_stale_stops=lambda: events.append(("sweep",)),
        _submit_close=lambda i, s: None,
        _position_qty=lambda i: 0.0,
        _history_complete=lambda i: None,
        _reattach_stop=lambda i, p: events.append(("reattach", i)),
        _stop_is_live=lambda o: False,
        _flatten=lambda i: (closed.append(str(i)), events.append(("flatten", str(i))))[0],
        _jevent=lambda kind, **kw: events.append((kind, kw.get("instrument"))),
        _venue_writes_allowed=lambda what="": True,
    )

    def risk_check():
        events.append(("risk",))
        if risk_raises:
            raise RuntimeError("journal locked")

    fake._risk_check = risk_check
    # F067: the watchdog also asks the venue what it holds.
    for n in ("_protection_watchdog", "_repair_orphan_positions", "_check_venue_inventory"):
        setattr(fake, n, getattr(NtxStrategy, n).__get__(fake, NtxStrategy))
    return fake, events, closed


def test_a_venue_position_with_no_state_is_closed_not_merely_reported():
    pos = SimpleNamespace(signed_qty=1.0, avg_px_open=100.0, quantity=1.0)
    fake, events, closed = _watchdog_fake(positions={A: [pos]}, states={})
    fake._protection_watchdog()
    assert closed == [A], f"orphan left open: {events}"
    assert ("orphan_position", A) in events, "and it must be a critical journal event"


def test_an_instrument_with_state_is_left_to_the_normal_repair_path():
    pos = SimpleNamespace(signed_qty=1.0, avg_px_open=100.0, quantity=1.0)
    state = SimpleNamespace(meta={})
    fake, _events, closed = _watchdog_fake(positions={A: [pos]}, states={A: state})
    fake._protection_watchdog()
    assert closed == [], "a tracked position is re-attached, never flattened"


def test_an_in_flight_entry_is_not_mistaken_for_an_orphan():
    """The state is created before the entry is submitted, but a fill can still
    arrive while orders are open. Do not close a position that is being opened."""
    pos = SimpleNamespace(signed_qty=1.0, avg_px_open=100.0, quantity=1.0)
    fake, _events, closed = _watchdog_fake(
        positions={A: [pos]}, states={}, open_orders=[SimpleNamespace(id="o1")]
    )
    fake._protection_watchdog()
    assert closed == []


def test_a_flat_account_closes_nothing():
    fake, _events, closed = _watchdog_fake(positions={}, states={})
    fake._protection_watchdog()
    assert closed == []


def test_a_raising_risk_check_does_not_abort_the_rest_of_the_pass():
    """The guards and the protection checks are independent; one must not take
    the other down (N2-07)."""
    pos = SimpleNamespace(signed_qty=1.0, avg_px_open=100.0, quantity=1.0)
    fake, events, closed = _watchdog_fake(positions={A: [pos]}, states={}, risk_raises=True)
    fake._protection_watchdog()
    assert ("sweep",) in events, "the stale-stop sweep was skipped"
    assert closed == [A], "the orphan repair was skipped"


def test_the_watchdog_docstring_is_a_docstring():
    """It had drifted below the first statement, which makes it a dead string."""
    assert (NtxStrategy._protection_watchdog.__doc__ or "").strip().startswith("Re-verify")
