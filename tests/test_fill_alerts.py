"""Phone alerts when a trade opens or closes.

Until now alerts only fired for things that were wrong -- a stale snapshot, an
unprotected position, a warning in the journal. The owner asked to be told when
the system actually trades, which is the ordinary case and by far the most
common reason to look at the dashboard.

The privacy contract does not bend for it (R1, docs/23 §7): a push topic is a
semi-public channel, so a fill alert carries the decision -- bot, side,
instrument, reason, percentages -- and never equity, PnL in dollars, sizes or
ids. A percentage tells the owner how the trade went and tells a stranger who
guessed the topic nothing about how much money is behind it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ntx.obs import alerts as A
from ntx.obs.journal import SCHEMA

OPENED = "2026-09-27T08:00:01+00:00"
CLOSED = "2026-09-27T12:00:02+00:00"


def _journal(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "journal-S003-live.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for r in rows:
        base = {
            "trade_id": "t-1",
            "correlation_id": "c-1",
            "strategy_id": "S003",
            "venue": "OKX",
            "instrument_id": "SOL-USDT-SWAP.OKX",
            "side": "LONG",
            "state": "open",
            "opened_at": OPENED,
            "qty": 0.09,
            "entry_price": 120.86,
            "stop_price": 115.6957,
            "notional_usd": 10.88,
            "is_demo": 0,
        }
        base.update(r)
        cols = ",".join(base)
        con.execute(
            f"INSERT INTO trades ({cols}) VALUES ({','.join('?' * len(base))})",
            tuple(base.values()),
        )
    con.commit()
    con.close()
    return path


def test_an_opened_trade_produces_an_alert(tmp_path):
    j = _journal(tmp_path, [{}])
    out = A.trade_alerts(j)
    assert [a.kind for a in out] == ["trade_opened"]
    assert "SOL-USDT-SWAP" in out[0].text
    assert "LONG" in out[0].text


def test_a_closed_trade_reports_the_move_and_the_reason(tmp_path):
    j = _journal(
        tmp_path,
        [{"state": "closed", "closed_at": CLOSED, "exit_price": 123.28,
          "exit_reason": "trailing_stop", "pnl_net_usd": 0.19}],
    )  # fmt: skip
    out = A.trade_alerts(j)
    assert [a.kind for a in out] == ["trade_closed"]
    text = out[0].text
    assert "trailing_stop" in text
    assert "+2.00%" in text, f"a +2% move must be readable at a glance: {text}"


def test_a_losing_trade_reads_as_a_loss(tmp_path):
    j = _journal(
        tmp_path,
        [{"state": "closed", "closed_at": CLOSED, "exit_price": 118.44,
          "exit_reason": "stop_loss"}],
    )  # fmt: skip
    assert "-2.00%" in A.trade_alerts(j)[0].text


def test_a_short_that_falls_is_a_win(tmp_path):
    j = _journal(
        tmp_path,
        [{"side": "SHORT", "state": "closed", "closed_at": CLOSED,
          "exit_price": 118.44, "exit_reason": "stop_loss"}],
    )  # fmt: skip
    assert "+2.00%" in A.trade_alerts(j)[0].text


def test_no_dollar_amounts_reach_the_push_topic(tmp_path):
    """docs/23 §7: never equity, PnL, sizes or ids."""
    j = _journal(
        tmp_path,
        [{"state": "closed", "closed_at": CLOSED, "exit_price": 123.28,
          "exit_reason": "trailing_stop", "pnl_net_usd": 0.19}],
    )  # fmt: skip
    line = A.redact(A.trade_alerts(j)[0])
    for forbidden in ("0.19", "0.09", "10.88", "t-1", "$"):
        assert forbidden not in line, f"{forbidden!r} leaked into {line!r}"


def test_the_cursor_stops_an_alert_arriving_twice(tmp_path):
    j = _journal(tmp_path, [{}])
    first = A.trade_alerts(j)
    assert first
    A.save_trade_cursor(tmp_path, "S003-live", A.trade_cursor_from(first))
    assert A.trade_alerts(j, since=A.load_trade_cursor(tmp_path, "S003-live")) == []


def test_a_close_is_still_announced_after_its_open_was(tmp_path):
    """One row becomes two alerts over its life; the open cursor must not eat the close."""
    j = _journal(tmp_path, [{}])
    A.save_trade_cursor(tmp_path, "S003-live", A.trade_cursor_from(A.trade_alerts(j)))
    con = sqlite3.connect(j)
    con.execute(
        "UPDATE trades SET state='closed', closed_at=?, exit_price=?, exit_reason=?",
        (CLOSED, 123.28, "trailing_stop"),
    )
    con.commit()
    con.close()
    out = A.trade_alerts(j, since=A.load_trade_cursor(tmp_path, "S003-live"))
    assert [a.kind for a in out] == ["trade_closed"]


def test_a_submitted_trade_that_never_filled_is_not_announced(tmp_path):
    j = _journal(tmp_path, [{"state": "submitted", "opened_at": None}])
    assert A.trade_alerts(j) == []


def test_fills_are_not_urgent(tmp_path):
    """A fill must never buzz like an unprotected position does."""
    j = _journal(tmp_path, [{}])
    assert A.trade_alerts(j)[0].severity == "info"


def test_an_unreadable_journal_does_not_raise(tmp_path):
    assert A.trade_alerts(tmp_path / "nope.db") == []
