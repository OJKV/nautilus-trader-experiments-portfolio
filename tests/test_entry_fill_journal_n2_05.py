"""N2-05 (round 2, PARTIAL in round 3): the journal went first on the entry fill.

`on_order_filled` called `journal.trade_state()` outside any try, BEFORE the
protective stop was submitted. A locked or corrupt journal therefore raised out
of the handler and the fill was left with no venue stop -- R54 says the journal
never goes first and never raises. Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy
from ntx.strategies.exits import Direction, PositionState

ADA = "ADA-USDT-SWAP.OKX"


def _quiet(*_a) -> None:
    return None


class BrokenJournal:
    def trade_state(self, trade_id):
        raise RuntimeError("database is locked")

    def transition(self, *a, **kw):
        raise RuntimeError("database is locked")

    def update_trade(self, *a, **kw):
        raise RuntimeError("database is locked")


def test_a_broken_journal_does_not_stop_the_stop():
    stops: list = []
    fake = SimpleNamespace(
        stop_client_order_ids={},
        _close_orders={},
        stop_orders={},
        journal=BrokenJournal(),
        log=SimpleNamespace(info=_quiet, warning=_quiet, error=_quiet, debug=_quiet),
        _stop_attached_at_entry=lambda: False,
        _stop_closes_position=lambda: True,
        _record_entry_fill=lambda state, event, trade_id: None,
        _submit_stop=lambda inst, state, qty: stops.append(qty),
    )
    fake.states = {
        ADA: PositionState(
            direction=Direction.LONG, entry_price=0.7, stop_price=0.665, margin_usd=9.0,
            meta={"entry_order_id": "E-1", "filled_qty": 0.0, "covered_qty": 0.0,
                  "trade_id": "t-1"},
        )
    }  # fmt: skip
    fake.on_order_filled = NtxStrategy.on_order_filled.__get__(fake, NtxStrategy)
    event = SimpleNamespace(
        instrument_id=ADA, client_order_id="E-1", last_qty=0.4, last_px=0.7, ts_event=1
    )
    fake.on_order_filled(event)  # must not raise
    assert stops == [0.4], "the fill has no venue stop because the journal raised (R54)"
