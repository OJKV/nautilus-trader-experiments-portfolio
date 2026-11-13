"""The dashboard's Net PnL is all time; the owner also wants today / 7d / 30d / 365d."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from ntx.obs.journal import SCHEMA
from ntx.ui.app import create_app


def _client(tmp_path, trades):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(tmp_path / "journal.db")
    con.executescript(SCHEMA)
    now = datetime.now(UTC)
    for i, (days_ago, pnl) in enumerate(trades):
        closed = (now - timedelta(days=days_ago, minutes=1)).isoformat()
        con.execute(
            "INSERT INTO trades (trade_id, correlation_id, strategy_id, venue, instrument_id,"
            " side, state, exit_reason, pnl_net_usd, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"t-{i}", f"c-{i}", "S003", "OKX", "SOL-USDT-SWAP.OKX", "LONG", "closed",
             "trailing_stop", pnl, closed),
        )  # fmt: skip
    con.commit()
    con.close()
    return TestClient(
        create_app(data_dir=tmp_path, logs_dir=tmp_path / "logs", reports_dir=tmp_path / "reports")
    )


def test_net_pnl_is_split_by_period(tmp_path):
    c = _client(tmp_path, [(0, 1.0), (3, 2.0), (20, -4.0), (200, 8.0)])
    p = c.get("/api/stats").json()["periods"]
    by = {r["period"]: r for r in p}
    assert by["7d"]["closed"] == 2 and by["7d"]["net_pnl_usd"] == 3.0
    assert by["30d"]["net_pnl_usd"] == -1.0
    assert by["365d"]["net_pnl_usd"] == 7.0
    assert by["all"]["net_pnl_usd"] == 7.0 and by["all"]["closed"] == 4
    assert by["today"]["closed"] <= 1  # a trade 1 min ago is today unless it is 00:00 UTC
