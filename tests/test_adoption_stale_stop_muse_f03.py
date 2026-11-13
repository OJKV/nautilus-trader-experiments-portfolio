"""Review 2026-10-11 F-03: adoption kept a venue stop's price without comparing it
with the stop THIS trade asked for (journal). Venue SHORT + live stop at a stale
0.150 + same-side journal row with stop 0.105 -> ADOPT at 0.150, no replacement:
the position runs at the risk an older trade's trigger implies (R71, F065 again).

Written before the fix (R41).
"""

from __future__ import annotations

from ntx.live.reconcile import VenuePosition, adoption_plan

DOGE = "DOGE-USDT-SWAP.OKX"


def _row(stop):
    return {"instrument_id": DOGE, "side": "SHORT", "qty": 100.0, "entry_price": 0.1,
            "stop_price": stop, "trade_id": "t-1"}  # fmt: skip


def test_a_venue_stop_far_from_the_trades_own_stop_is_flagged():
    plan = adoption_plan([VenuePosition(DOGE, -100.0, 0.1, True)], [_row(0.105)], {DOGE: 0.150})
    a = plan[0]
    assert a.journal_stop == 0.105, "the trade's own stop must reach the caller"


def test_a_matching_venue_stop_is_not_flagged():
    plan = adoption_plan([VenuePosition(DOGE, -100.0, 0.1, True)], [_row(0.105)], {DOGE: 0.10505})
    assert plan[0].journal_stop is None


# ---- what adoption does with the flag -------------------------------------- #

from types import SimpleNamespace  # noqa: E402

from ntx.live.reconcile import AdoptAction, Adoption  # noqa: E402
from ntx.strategies.base import NtxStrategy  # noqa: E402
from ntx.strategies.signal import Direction  # noqa: E402


def _fake(sync_pct=0.0):
    calls, events = [], []
    fake = SimpleNamespace(
        instruments={DOGE: SimpleNamespace(multiplier=1)},
        exit_cfg=SimpleNamespace(tsl_enabled=False),
        config=SimpleNamespace(max_leverage=3.0, is_demo=False, tsl_sync_min_price_pct=sync_pct),
        journal=None, states={}, stop_orders={}, stop_client_order_ids={},
        venue_stop_hook=None, id="S003",
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"))),
        _replace_stale_stop=lambda inst, st, venue_price, wanted: calls.append(
            (venue_price, wanted, st.stop_price)) or True,
        _submit_stop=lambda *a: calls.append(("submit",)),
    )  # fmt: skip
    return fake, calls, events


def _adoption():
    return Adoption(DOGE, AdoptAction.ADOPT, "journal + venue agree", Direction.SHORT, 100.0,
                    0.1, 0.150, False, "t-1", False, 0.105)  # fmt: skip


def test_a_stale_live_stop_is_replaced_at_the_trades_own_price():
    fake, calls, _events = _fake()
    live = SimpleNamespace(client_order_id="C-1")
    NtxStrategy._adopt(fake, DOGE, _adoption(), live)
    assert calls == [(0.150, 0.105, 0.105)]
    assert fake.states[DOGE].stop_price == 0.105


def test_with_venue_trailing_on_a_moved_stop_is_reported_not_replaced():
    fake, calls, events = _fake(sync_pct=0.5)
    NtxStrategy._adopt(fake, DOGE, _adoption(), SimpleNamespace(client_order_id="C-1"))
    assert calls == [] and fake.states[DOGE].stop_price == 0.150
    assert ("adoption_stop_diverges", "warning") in events
