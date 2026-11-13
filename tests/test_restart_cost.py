"""The disclosure that replaced the flatness refusal.

If a deploy is going to run over an open position, the operator gets the whole
cost in front of them first -- every position it touches, the stop it will
cancel and re-place, and how long that has actually taken on this account.
Measured, never estimated (R11): a guessed number above a real decision is worse
than no number, because it reads as evidence.
"""

from __future__ import annotations

import sqlite3

from ntx.obs.journal import SCHEMA
from ntx.ops.restart_cost import gap_sentence, protect_gaps, report

POS = [{"instId": "SOL-USDT-SWAP", "pos": "0.09"}, {"instId": "LINK-USDT-SWAP", "pos": "0.6"}]
STOPS = {"SOL-USDT-SWAP": "115.696", "LINK-USDT-SWAP": "13.284"}


def test_a_flat_account_says_so_plainly():
    assert report([], {}, [300]) == "The account is flat. A restart touches no position."


def test_every_position_and_its_stop_is_named():
    out = report(POS, STOPS, [296, 304, 356])
    assert "SOL-USDT-SWAP" in out and "115.696" in out
    assert "LINK-USDT-SWAP" in out and "13.284" in out
    assert "2 open position(s)" in out


def test_the_window_is_measured_not_estimated():
    out = report(POS, STOPS, [296, 304, 356])
    assert "median 304 ms" in out
    assert "worst 356 ms" in out
    assert "3 recorded fill(s)" in out


def test_no_measurement_is_stated_as_unknown_not_as_small():
    """R70 again: absence of a number is not a small number."""
    out = gap_sentence([])
    assert "unknown" in out
    assert "ms" not in out


def test_a_position_with_no_venue_stop_is_called_out():
    out = report(POS, {"SOL-USDT-SWAP": "115.696"}, [300])
    assert "NO VENUE STOP FOUND" in out
    assert "already unprotected" in out


def test_gaps_come_from_the_journal(tmp_path):
    path = tmp_path / "journal.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for i, gap in enumerate((304, 296, 356)):
        con.execute(
            "INSERT INTO trades (trade_id, correlation_id, strategy_id, venue, "
            "instrument_id, side, state, protect_gap_ms) VALUES (?,?,?,?,?,?,?,?)",
            (f"t-{i}", f"c-{i}", "S003", "OKX", "SOL-USDT-SWAP.OKX", "LONG", "closed", gap),
        )
    con.commit()
    con.close()
    assert protect_gaps(path) == [296, 304, 356]


def test_a_missing_journal_is_not_an_error(tmp_path):
    assert protect_gaps(tmp_path / "nope.db") == []
