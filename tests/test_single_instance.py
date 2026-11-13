"""One node per strategy per account (F046). Written before the code (R41).

Two nodes with the same trader id fought over one position on the demo account:
the second adopted what the first owned, OKX refused the duplicate stop ("only 1
TP/SL"), and the guard flattened. Nothing was lost, but a lock is cheaper than a
race -- and on the VPS a stray `systemctl start` plus a manual run is the same
mistake.
"""

from __future__ import annotations

import pytest

from ntx.live.lock import AlreadyRunning, single_instance


def test_second_instance_is_refused(tmp_path):
    def second():
        with single_instance("NTX-900", tmp_path):
            pass

    with single_instance("NTX-900", tmp_path), pytest.raises(AlreadyRunning, match="NTX-900"):
        second()


def test_lock_is_released_on_exit(tmp_path):
    with single_instance("NTX-900", tmp_path):
        pass
    with single_instance("NTX-900", tmp_path):
        pass  # no exception: the first one let go


def test_lock_is_released_even_if_the_node_crashes(tmp_path):
    with pytest.raises(RuntimeError), single_instance("NTX-900", tmp_path):
        raise RuntimeError("node blew up")
    with single_instance("NTX-900", tmp_path):
        pass


def test_different_strategies_do_not_block_each_other(tmp_path):
    with single_instance("NTX-900", tmp_path), single_instance("NTX-003", tmp_path):
        pass


def test_a_stale_lock_file_from_a_dead_process_is_taken_over(tmp_path):
    """A kill -9 leaves the file behind; the next start must not be blocked."""
    (tmp_path / "NTX-900.lock").write_text("999999")  # a pid that cannot be running
    with single_instance("NTX-900", tmp_path):
        pass


def test_node_main_takes_the_lock():
    import inspect

    from ntx.live import node

    assert "single_instance" in inspect.getsource(node.main)


# ------------------------------------------------------------------ F047 ---- #
def test_position_closed_by_someone_else_closes_the_journal_row(tmp_path):
    """Drill 2026-09-18: a stray flatten closed the position; the strategy
    dropped its state but left the journal row `open` forever. The venue is the
    truth (R27) -- if it says flat, the trade is over."""
    from types import SimpleNamespace

    from ntx.obs.journal import Journal
    from ntx.strategies.base import NtxStrategy
    from ntx.strategies.exits import PositionState
    from ntx.strategies.signal import Direction

    j = Journal(tmp_path / "j.db")
    j.open_trade(trade_id="t1", correlation_id="c", strategy_id="S", venue="OKX",
                 instrument_id="X", side="LONG", qty=1.0, entry_price=100.0)  # fmt: skip
    st = PositionState(
        Direction.LONG, 100.0, 95.0, 10.0, base_units=1.0,
        meta={"trade_id": "t1", "filled_qty": 1.0, "entry_qty": 1.0, "entry_notional": 100.0},
    )  # fmt: skip
    fake = SimpleNamespace(
        states={"X": st},
        stop_orders={},
        stop_client_order_ids={},
        _pending_entry={},
        journal=j,
        exit_counts={},
        _closing={},
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        _cancel_protective_stop=lambda inst: None,
        _last_close=lambda inst: 101.0,
        _equity_usd=lambda: 100.0,
        _fee=NtxStrategy._fee,
        _entry_px=NtxStrategy._entry_px,
        _pnl=NtxStrategy._pnl,
    )
    fake._close_journal_trade = NtxStrategy._close_journal_trade.__get__(fake, NtxStrategy)
    NtxStrategy.on_position_closed(fake, SimpleNamespace(instrument_id="X"))
    assert "X" not in fake.states
    row = j.closed_trades()[0]
    assert row["exit_reason"] == "closed_externally"
    assert row["exit_price"] == pytest.approx(101.0)
