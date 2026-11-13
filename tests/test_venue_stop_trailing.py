"""The venue stop must follow the trailing stop (review B-08, G8).

Until now the stop sitting at OKX stayed at its initial 2xATR level for the whole
life of a trade, while the trailing stop existed only in Python and was evaluated
once per 4h bar. If the node was down -- a deploy, a crash, a reboot -- a trade
that was +15% could give all of it back to the original stop. It is the largest
behavioural gap between the backtest and the live bot while it is unattended.

Written before the code (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy
from ntx.strategies.exits import PositionState, TrailingStop
from ntx.strategies.signal import Direction


def _fake(direction=Direction.LONG, entry=100.0, stop=95.0, **over):
    calls = SimpleNamespace(cancelled=[], placed=[], events=[])
    trailing = TrailingStop(direction, entry, activation_pct=1.5, trail_pct=1.0)
    st = PositionState(
        direction, entry, stop, 10.0, base_units=1.0,
        trailing=trailing, meta={"trade_id": "t1", "filled_qty": 1.0, "covered_qty": 1.0},
    )  # fmt: skip
    fake = SimpleNamespace(
        states={"X": st},
        stop_orders={"X": SimpleNamespace(client_order_id="stop-1")},
        stop_client_order_ids={"stop-1": "X"},
        journal=None,
        config=SimpleNamespace(trading_enabled=True, tsl_sync_min_price_pct=0.25),
        exit_cfg=SimpleNamespace(tsl_enabled=True),
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        _cancel_protective_stop=lambda inst: calls.cancelled.append(inst),
        _submit_stop=lambda inst, state, qty: calls.placed.append((inst, state.stop_price)),
        _jevent=lambda kind, **kw: calls.events.append(kind),
        _jwrite=lambda what, fn: None,
    )
    fake.__dict__.update(over)
    fake._sync_venue_stop = NtxStrategy._sync_venue_stop.__get__(fake, NtxStrategy)
    fake._venue_writes_allowed = NtxStrategy._venue_writes_allowed.__get__(fake, NtxStrategy)
    return fake, calls, st


def test_the_venue_stop_follows_a_materially_improved_trailing_level():
    fake, calls, st = _fake()
    st.trailing.update(120.0)  # +20%: armed, level ~118.8
    assert st.trailing.level is not None and st.trailing.level > st.stop_price
    fake._sync_venue_stop("X", st)
    assert calls.cancelled == ["X"], "the old stop must be cancelled"
    assert calls.placed and calls.placed[0][1] == st.trailing.level
    assert st.stop_price == st.trailing.level, "state must agree with the venue"


def test_the_cancel_and_the_replace_are_adjacent():
    """L-10's rule: never cancel protection you are not about to replace."""
    order = []
    fake, _calls, st = _fake(
        _cancel_protective_stop=lambda inst: order.append("cancel"),
        _submit_stop=lambda inst, state, qty: order.append("place"),
    )
    st.trailing.update(120.0)
    fake._sync_venue_stop("X", st)
    assert order == ["cancel", "place"]


def test_a_trivial_improvement_is_not_worth_a_round_trip():
    """Every move is cancel + place at the venue, on every instrument, every bar.
    Churn costs rate limit and re-opens the unprotected window for nothing."""
    fake, calls, st = _fake(stop=100.4)
    st.trailing.update(101.6)  # armed, level 100.584 -- only 0.18% better than 100.4
    fake._sync_venue_stop("X", st)
    assert calls.placed == [] and calls.cancelled == []


def test_the_stop_never_moves_against_the_position():
    fake, calls, st = _fake(direction=Direction.SHORT, entry=100.0, stop=105.0)
    st.trailing.update(80.0)  # short in profit: level ~80.8, well below 105
    fake._sync_venue_stop("X", st)
    assert calls.placed and calls.placed[0][1] < 105.0
    # A later adverse move must not loosen it.
    calls.placed.clear()
    calls.cancelled.clear()
    st.trailing.update(95.0)
    fake._sync_venue_stop("X", st)
    assert calls.placed == [], "a trailing stop that loosens is not a trailing stop"


def test_an_unarmed_trailing_stop_changes_nothing():
    fake, calls, st = _fake()
    st.trailing.update(100.5)  # below the 1.5% activation
    fake._sync_venue_stop("X", st)
    assert calls.placed == [] and calls.cancelled == []


def test_observe_mode_never_touches_the_venue_stop():
    fake, calls, st = _fake()
    fake.config = SimpleNamespace(trading_enabled=False, tsl_sync_min_price_pct=0.25)
    st.trailing.update(120.0)
    fake._sync_venue_stop("X", st)
    assert calls.cancelled == [] and calls.placed == []


def test_the_bar_handler_syncs_a_position_it_keeps():
    """It has to run where the trailing level is updated: the bar close."""
    import inspect

    src = inspect.getsource(NtxStrategy.on_bar)
    assert "_sync_venue_stop" in src
    assert src.index("evaluate_exit") < src.index("_sync_venue_stop")
