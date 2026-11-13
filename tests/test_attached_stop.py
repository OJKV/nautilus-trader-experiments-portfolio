"""Attached stop-loss: the venue stop is created WITH the entry (G7).

Measured on demo 2026-09-18 (`ops/bracket_probe.py --side SHORT`): the child stop
was accepted 38 ms BEFORE the entry fill, later triggered and filled, and could be
cancelled through nautilus. That removes the 449-520 ms window that fill-then-attach
leaves (F007). Written before the code (R41).

Off by default: a venue-side stop can trigger INTRABAR, while the validated S003
backtest exits at bar close -- switching changes execution semantics, so the gate
must be re-run before it becomes the default.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_live_node import _settings

from ntx.live import node
from ntx.strategies.base import NtxStrategy, NtxStrategyConfig
from ntx.strategies.exits import PositionState
from ntx.strategies.signal import Direction

SOL = "SOL-USDT-SWAP.OKX"


# ------------------------------------------------------------------ flag ---- #
def test_attached_stop_is_off_by_default():
    assert NtxStrategyConfig(instrument_ids=["X.OKX"]).venue_attaches_stop is False


def test_node_passes_the_flag_from_settings(tmp_path):
    s = _settings(tmp_path, NTX_ATTACH_STOP_AT_ENTRY="true")
    assert node.strategy_kwargs(node.plan(s), s)["venue_attaches_stop"] is True
    s2 = _settings(tmp_path)
    assert node.strategy_kwargs(node.plan(s2), s2)["venue_attaches_stop"] is False


# ----------------------------------------------------------------- entry ---- #
def _entry_fake(attached: bool):
    calls = SimpleNamespace(lists=[], orders=[], stops=[])
    sl = SimpleNamespace(client_order_id="sl-1")
    tp = SimpleNamespace(client_order_id="tp-1")
    parent = SimpleNamespace(client_order_id="entry-1")

    def bracket(**kw):
        calls.lists.append(kw)
        return SimpleNamespace(orders=[parent, sl, tp], first=parent)

    fake = SimpleNamespace(
        config=SimpleNamespace(venue_attaches_stop=attached, max_leverage=3.0),
        instruments={SOL: SimpleNamespace(make_qty=lambda q: q, make_price=lambda p: p)},
        order_factory=SimpleNamespace(
            bracket=bracket,
            market=lambda **kw: calls.orders.append(kw) or parent,
        ),
        submit_order_list=lambda ol: calls.lists.append("submitted"),
        submit_order=lambda o, params=None: calls.orders.append(o),
        stop_orders={},
        stop_client_order_ids={},
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
    )
    fake._stop_attached_at_entry = lambda: fake.config.venue_attaches_stop
    return fake, calls, parent, sl


def test_attached_entry_submits_one_bracket_and_tracks_the_child_stop():
    fake, calls, parent, sl = _entry_fake(attached=True)
    entry_id = NtxStrategy._submit_entry(fake, SOL, Direction.LONG, qty=0.5, stop_price=95.0)
    assert "submitted" in calls.lists, "a bracket (entry + stop) must be sent as one list"
    kw = calls.lists[0]
    assert kw["sl_trigger_price"] == 95.0
    assert entry_id == parent.client_order_id
    assert fake.stop_orders[SOL] is sl
    assert fake.stop_client_order_ids[sl.client_order_id] == SOL


def test_plain_entry_sends_a_market_order_and_no_stop():
    fake, calls, parent, _sl = _entry_fake(attached=False)
    entry_id = NtxStrategy._submit_entry(fake, SOL, Direction.LONG, qty=0.5, stop_price=95.0)
    assert calls.lists == [] and entry_id == parent.client_order_id
    assert fake.stop_orders == {}, "fill-then-attach only protects after the fill"


def test_take_profit_child_is_parked_far_away():
    """nautilus's bracket factory requires a TP child; it must never be the exit
    that fires -- S003's ROI/TP exits are OFF (ADR-0008)."""
    fake, calls, _p, _s = _entry_fake(attached=True)
    NtxStrategy._submit_entry(fake, SOL, Direction.LONG, qty=0.5, stop_price=95.0)
    kw = calls.lists[0]
    assert kw["tp_trigger_price"] >= 100.0 * 1.4, kw["tp_trigger_price"]


def test_short_entry_puts_the_stop_above_and_the_target_below():
    fake, calls, _p, _s = _entry_fake(attached=True)
    NtxStrategy._submit_entry(fake, SOL, Direction.SHORT, qty=0.5, stop_price=105.0)
    kw = calls.lists[0]
    assert kw["sl_trigger_price"] == 105.0
    assert kw["tp_trigger_price"] < 105.0


def test_attached_stop_is_not_topped_up_on_fills():
    """The OCO child already covers the whole entry quantity."""
    import inspect

    src = inspect.getsource(NtxStrategy.on_order_filled)
    assert "_stop_attached_at_entry" in src


# --------------------------------------------------------------- restart ---- #
OUR_OCO = {
    "algoId": "oco1",
    "slTriggerPx": "95",
    "tpTriggerPx": "150",
    "sz": "0.5",
    "reduceOnly": "true",
    "closeFraction": "",
}
FOREIGN = {"algoId": "x1", "tpTriggerPx": "120", "slTriggerPx": "", "reduceOnly": "false"}


def _vs(monkeypatch, pending, post_result=None):
    from ntx.ops import venue_stops

    sent = []
    monkeypatch.setattr(
        venue_stops,
        "_signed_get",
        lambda base, path, creds, demo: {
            "code": "0",
            "data": pending.get("oco" if "ordType=oco" in path else "conditional", []),
        },
    )
    monkeypatch.setattr(
        venue_stops,
        "_signed_post",
        lambda base, path, body, creds, demo: (
            sent.append(body) or (post_result or {"code": "0", "data": [{"sCode": "0"}]})
        ),
    )
    return venue_stops, sent


def test_restart_releases_our_attached_oco_stop(monkeypatch, tmp_path):
    """F044 again: a restart must see the OCO child, not just a conditional stop."""
    calls = {"n": 0}
    from ntx.ops import venue_stops

    def get(base, path, creds, demo):
        if "ordType=oco" in path:
            calls["n"] += 1
            return {"code": "0", "data": [OUR_OCO] if calls["n"] == 1 else []}
        return {"code": "0", "data": []}

    monkeypatch.setattr(venue_stops, "_signed_get", get)
    monkeypatch.setattr(
        venue_stops, "_signed_post", lambda *a, **k: {"code": "0", "data": [{"sCode": "0"}]}
    )
    assert venue_stops.release_stop(SOL, _settings(tmp_path), True) == 95.0


def test_a_foreign_trigger_order_still_aborts(monkeypatch, tmp_path):
    vs, sent = _vs(monkeypatch, {"oco": [OUR_OCO], "conditional": [FOREIGN]})
    with pytest.raises(RuntimeError, match="not our"):
        vs.release_stop(SOL, _settings(tmp_path), True)
    assert sent == []


def test_two_of_our_own_stops_is_ambiguous(monkeypatch, tmp_path):
    vs, _sent = _vs(monkeypatch, {"oco": [OUR_OCO, dict(OUR_OCO, algoId="oco2")]})
    with pytest.raises(RuntimeError, match="expected one"):
        vs.release_stop(SOL, _settings(tmp_path), True)


# ------------------------------------------------------- partial entry ----- #
def test_partial_entry_fill_leaves_the_attached_stop_alone(tmp_path):
    """The OCO sz is the ORDERED quantity; if the entry only partly fills the
    stop over-covers, which reduce-only makes harmless -- and OKX allows one
    such order per position, so a top-up would be rejected."""
    from test_live_path import _fake_strategy, _fill

    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, meta={"trade_id": "t1"})
    fake, calls, _j = _fake_strategy(tmp_path, st)
    fake._stop_attached_at_entry = lambda: True
    fake.stop_orders["X"] = SimpleNamespace(client_order_id="sl-1")  # _fake_strategy uses "X"
    NtxStrategy.on_order_filled(fake, _fill("entry", 1.0))
    NtxStrategy.on_order_filled(fake, _fill("entry", 2.0))
    assert calls.submitted == [], "no second stop while the attached one is live"


# ------------------------------------------------- drill polish (2026-09-18) - #
def test_attached_stop_records_a_zero_protect_gap(tmp_path):
    """With the stop attached there is no 'accepted after the fill' moment, so
    the gap column stayed NULL -- and `protect_gap_ms` is how F007's window is
    tracked. Attached protection is a measured zero, not a blank."""
    from test_live_path import _fake_strategy, _fill

    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, meta={"trade_id": "t1"})
    fake, _calls, j = _fake_strategy(tmp_path, st)
    fake._stop_attached_at_entry = lambda: True
    fake.stop_orders["X"] = SimpleNamespace(client_order_id="sl-1")
    fake.stop_client_order_ids["sl-1"] = "X"
    NtxStrategy.on_order_filled(fake, _fill("entry", 1.0))
    assert j.open_trades()[0]["protect_gap_ms"] == 0


def test_the_venues_own_cancel_at_exit_is_not_treated_as_sabotage(tmp_path):
    """OKX cancels the OCO child when the position closes. That is the exit
    working, not someone removing our protection."""
    from test_live_path import _fake_strategy

    st = PositionState(
        Direction.LONG,
        100.0,
        95.0,
        10.0,
        meta={"trade_id": "t1", "closing": "time_stop", "filled_qty": 1.0},
    )
    fake, calls, _j = _fake_strategy(tmp_path, st)
    fake.stop_orders["X"] = SimpleNamespace(client_order_id="sl-1")
    fake.stop_client_order_ids["sl-1"] = "X"
    fake.unprotected_events = 0
    fake._has_position = lambda inst: True
    NtxStrategy.on_order_canceled(fake, SimpleNamespace(client_order_id="sl-1", instrument_id="X"))
    assert calls.submitted == [], "no re-attach while the position is being closed"
    assert fake.unprotected_events == 0
