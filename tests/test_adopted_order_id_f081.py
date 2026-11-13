"""F081: an adopted position crashed the node on the next OrderCanceled.

S004-demo, 2026-10-07 04:00:02. Adoption stores `entry_order_id = "adopted"`
(a str); nautilus's Cython `ClientOrderId` raises TypeError when compared with a
str, so `_entry_canceled` raised, `shutdown_on_error` stopped the whole node one
second after it had submitted an order. Live adopts on every restart too.

Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.identifiers import ClientOrderId

from ntx.strategies.base import NtxStrategy
from ntx.strategies.exits import Direction, PositionState

ADA = "ADA-USDT-SWAP.OKX"


def _fake():
    state = PositionState(
        direction=Direction.LONG, entry_price=0.8, stop_price=0.7, margin_usd=1.0,
        meta={"entry_order_id": "adopted", "filled_qty": 3.07},
    )  # fmt: skip
    return SimpleNamespace(
        states={ADA: state},
        journal=None,
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
    )


def test_a_cancel_event_with_an_adopted_position_open_does_not_raise():
    fake = _fake()
    event = SimpleNamespace(
        client_order_id=ClientOrderId("O-20261007-040001-001"), instrument_id=ADA
    )
    NtxStrategy._entry_canceled(fake, event)  # raised TypeError
    assert ADA in fake.states, "an unrelated cancel must not touch the adopted position"


def test_a_fill_on_an_adopted_position_is_treated_as_a_closing_fill():
    """on_order_filled compared the same way: ClientOrderId != "adopted" raised."""
    from ntx.strategies.base import _same_order

    assert not _same_order(ClientOrderId("O-1"), "adopted")
    assert _same_order(ClientOrderId("O-1"), ClientOrderId("O-1"))
    assert _same_order("O-1", ClientOrderId("O-1"))
    assert not _same_order(None, ClientOrderId("O-1"))
