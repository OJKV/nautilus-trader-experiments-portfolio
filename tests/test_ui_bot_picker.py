"""The picker lists bots you can watch, not every journal ever written.

S900 is the demo rehearsal strategy: it runs for a few minutes, exercises the
live path, and stops. Its journal stays on disk forever (R29) and the picker
listed it from that alone -- a permanent entry that has no snapshot, shows
nothing, and reads as a broken bot rather than a finished experiment.

A bot with no state snapshot is not something the monitor can monitor. The
journal is still there and still reachable by asking for it explicitly; it just
does not belong in a live dashboard's dropdown.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from ntx.obs.journal import SCHEMA
from ntx.ui.app import create_app


def _box(tmp_path, *, snapshots=(), journals=()):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    for name in snapshots:
        (tmp_path / "state" / f"{name}.json").write_text(
            json.dumps({"ts_ns": 0, "strategy_id": name.split("-")[0], "is_demo": True})
        )
    for name in journals:
        sid, env = name.rsplit("-", 1)
        stem = "journal.db" if env == "live" else f"journal-{env}-{sid}.db"
        con = sqlite3.connect(tmp_path / stem)
        con.executescript(SCHEMA)
        con.close()
    return TestClient(
        create_app(data_dir=tmp_path, logs_dir=tmp_path / "logs", reports_dir=tmp_path / "reports")
    )


def test_a_journal_without_a_snapshot_is_not_in_the_picker(tmp_path):
    c = _box(tmp_path, snapshots=["S003-demo"], journals=["S003-demo", "S900-demo"])
    ids = [b["id"] for b in c.get("/api/bots").json()["bots"]]
    assert "S003-demo" in ids
    assert "S900-demo" not in ids, "a finished rehearsal is not a bot you can watch"


def test_all_still_shows_everything(tmp_path):
    c = _box(tmp_path, snapshots=["S003-demo"], journals=["S003-demo", "S900-demo"])
    ids = [b["id"] for b in c.get("/api/bots?all=1").json()["bots"]]
    assert "S900-demo" in ids


def test_a_hidden_bot_is_still_reachable_by_name(tmp_path):
    """Hiding it from the dropdown must not delete its history (R29)."""
    c = _box(tmp_path, snapshots=["S003-demo"], journals=["S003-demo", "S900-demo"])
    assert c.get("/api/trades?bot=S900-demo").status_code == 200


def test_a_bot_with_a_snapshot_is_listed_even_when_stale(tmp_path):
    """A node that died IS something to watch -- that is the alarm, not noise."""
    c = _box(tmp_path, snapshots=["S003-demo"], journals=["S003-demo"])
    bots = {b["id"]: b for b in c.get("/api/bots").json()["bots"]}
    # S003-live is ALWAYS_LISTED whether or not it has written anything.
    assert set(bots) == {"S003-demo", "S003-live"}
    assert bots["S003-demo"]["stale"] is True


def test_the_live_bot_is_never_hidden(tmp_path):
    """Its absence is the alarm, not a reason to stop showing it.

    S003-live is in ALWAYS_LISTED so a money node that has never written a
    snapshot -- because it is down, or was never started -- is still visible and
    still reads as broken. Filtering the picker must not take that away.
    """
    c = _box(tmp_path, snapshots=[], journals=[])
    assert [b["id"] for b in c.get("/api/bots").json()["bots"]] == ["S003-live"]
