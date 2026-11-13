"""Trades the strategy ended, and trades something else ended, are different data.

2026-10-06, reading the three bots:

    S003-demo   12 closed, win rate 8%, PF 0.01, -1.29%
    S003-live    5 closed, win rate 40%, PF 0.29, -0.42%
    S004-demo   10 closed, win rate 70%, PF 3.64, +0.99%   (all trailing_stop)

S003 looks broken and S004 looks excellent. But S003's exit mix is:

    closed_while_offline 4, stop_refused 1   <- our bugs (F065/F066/F069)
    trailing_stop 3, stop_loss 4             <- the strategy

Five of twelve demo exits, and two of five LIVE exits, were ended by an incident,
not by a decision the strategy made. Those trades say nothing about whether the
edge is real, and averaging them in means every report of S003 is polluted --
including any judgement about whether to keep running it.

The money is real either way, so the headline total must stay honest. What was
missing is the ability to ask the other question: *of the trades the strategy
actually finished, how did it do?*

So: totals unchanged, plus a `strategy_only` block over exits the strategy chose,
and a visible count of the ones it did not.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from ntx.obs.journal import SCHEMA
from ntx.ui.app import create_app


def _box(tmp_path, trades):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(tmp_path / "journal.db")
    con.executescript(SCHEMA)
    for i, (reason, pnl) in enumerate(trades):
        con.execute(
            "INSERT INTO trades (trade_id, correlation_id, strategy_id, venue, instrument_id,"
            " side, state, exit_reason, pnl_net_usd) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"t-{i}", f"c-{i}", "S003", "OKX", "SOL-USDT-SWAP.OKX", "LONG", "closed", reason, pnl),
        )
    con.commit()
    con.close()
    return TestClient(
        create_app(data_dir=tmp_path, logs_dir=tmp_path / "logs", reports_dir=tmp_path / "reports")
    )


# The real S003-demo mix: the strategy made money, the incidents lost it.
MIXED = [
    ("trailing_stop", 2.0),
    ("trailing_stop", 1.0),
    ("stop_loss", -0.5),
    ("closed_while_offline", -10.0),
    ("stop_refused", -5.0),
]


def test_the_headline_total_still_counts_every_trade(tmp_path):
    """The money was real whoever ended the trade."""
    s = _box(tmp_path, MIXED).get("/api/stats").json()
    assert round(s["net_pnl_usd"], 6) == -12.5
    assert s["closed"] == 5


def test_strategy_only_excludes_trades_an_incident_ended(tmp_path):
    s = _box(tmp_path, MIXED).get("/api/stats").json()["strategy_only"]
    assert s["closed"] == 3
    assert round(s["net_pnl_usd"], 6) == 2.5
    assert s["wins"] == 2 and s["losses"] == 1


def test_the_two_views_disagree_loudly_when_it_matters(tmp_path):
    """This is the whole point: -12.5 overall, +2.5 from the strategy."""
    full = _box(tmp_path, MIXED).get("/api/stats").json()
    assert full["net_pnl_usd"] < 0 < full["strategy_only"]["net_pnl_usd"]


def test_the_non_strategy_exits_are_counted_not_hidden(tmp_path):
    s = _box(tmp_path, MIXED).get("/api/stats").json()
    assert s["non_strategy_exits"] == 2
    assert set(s["non_strategy_exit_reasons"]) == {"closed_while_offline", "stop_refused"}


def test_a_clean_record_reads_the_same_both_ways(tmp_path):
    """S004: ten trades, all trailing_stop, nothing to separate."""
    clean = [("trailing_stop", 1.0)] * 10
    s = _box(tmp_path, clean).get("/api/stats").json()
    assert s["non_strategy_exits"] == 0
    assert s["strategy_only"]["net_pnl_usd"] == s["net_pnl_usd"]
    assert s["strategy_only"]["closed"] == s["closed"]


def test_a_risk_guard_exit_is_not_the_strategy(tmp_path):
    """A KILL is a deliberate, correct action -- and still not evidence of edge."""
    s = _box(tmp_path, [("kill_switch", -3.0), ("trailing_stop", 1.0)]).get("/api/stats").json()
    assert s["strategy_only"]["closed"] == 1
    assert s["non_strategy_exits"] == 1


def test_an_unknown_reason_is_treated_as_not_the_strategy(tmp_path):
    """Fail safe: an exit we cannot name is not evidence for the strategy."""
    s = _box(tmp_path, [(None, -1.0), ("trailing_stop", 1.0)]).get("/api/stats").json()
    assert s["strategy_only"]["closed"] == 1
