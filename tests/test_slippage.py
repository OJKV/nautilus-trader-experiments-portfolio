"""Record what the fill actually cost against the price the signal used (O-06f).

The soak exists to compare live behaviour with the backtest, and the single
biggest unmeasured assumption is slippage: the gate charges **5 bps per side**
at live config. The journal has had a `slippage_bps` column since day one and
the monitor displays it, but nothing ever wrote it -- so after four real trades
we still could not say whether 5 bps was generous or optimistic.

Signed so that POSITIVE always means adverse: paying more than the signal price
for a long, receiving less for a short.

Written before the code (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ntx.strategies.base import NtxStrategy
from ntx.strategies.signal import Direction


def test_a_long_filled_above_the_signal_price_is_positive_slippage():
    # signal decided at 100.0, filled at 100.05 -> 5 bps adverse
    assert NtxStrategy._slippage_bps(Direction.LONG, 100.0, 100.05) == pytest.approx(5.0)


def test_a_long_filled_below_the_signal_price_is_negative_slippage():
    """Price improvement is real and must not be hidden: averaging only the bad
    fills would overstate the cost of trading."""
    assert NtxStrategy._slippage_bps(Direction.LONG, 100.0, 99.95) == pytest.approx(-5.0)


def test_a_short_filled_below_the_signal_price_is_positive_slippage():
    assert NtxStrategy._slippage_bps(Direction.SHORT, 100.0, 99.9) == pytest.approx(10.0)


def test_a_nonsense_reference_price_yields_nothing_rather_than_infinity():
    for ref in (0.0, -1.0, None):
        assert NtxStrategy._slippage_bps(Direction.LONG, ref, 100.0) is None


def test_the_entry_fill_writes_slippage_to_the_journal(tmp_path):
    from ntx.obs.journal import Journal
    from ntx.strategies.exits import PositionState

    j = Journal(tmp_path / "j.db")
    j.open_trade(trade_id="t1", correlation_id="c", strategy_id="S", venue="OKX",
                 instrument_id="X", side="LONG", qty=1.0, entry_price=100.0)  # fmt: skip
    st = PositionState(
        Direction.LONG,
        100.0,  # the signal bar close the size was computed from
        95.0,
        10.0,
        base_units=1.0,
        meta={"trade_id": "t1", "qty": "1", "filled_qty": 0.0, "covered_qty": 0.0},
    )
    fake = SimpleNamespace(
        states={"X": st},
        journal=j,
        stop_orders={},
        stop_client_order_ids={},
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        _fee=lambda ev: 0.05,
        _entry_px=NtxStrategy._entry_px,
        _slippage_bps=NtxStrategy._slippage_bps,
    )
    fake._record_entry_fill = NtxStrategy._record_entry_fill.__get__(fake, NtxStrategy)
    fake._record_entry_fill(
        st, SimpleNamespace(last_qty=1.0, last_px=100.05, instrument_id="X"), "t1"
    )
    with j._conn() as c:
        row = c.execute(
            "SELECT entry_price, slippage_bps FROM trades WHERE trade_id='t1'"
        ).fetchone()
    assert row["entry_price"] == pytest.approx(100.05)
    assert row["slippage_bps"] == pytest.approx(5.0), "the soak must measure what fills cost"
