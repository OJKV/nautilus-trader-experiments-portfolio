"""NTX Monitor (Phase 3.5) -- the separate, read-only web UI process.

Written before the code (R41). Everything is fabricated in tmp dirs: snapshot
JSON, journals created through the real `Journal` API, and JSON log lines.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ntx.obs.journal import Journal

NOW = datetime(2026, 9, 17, 16, 0, 30, tzinfo=UTC)


def _snapshot(ts: datetime, **over) -> dict:
    snap = {
        "schema": 1,
        "ts": ts.isoformat(),
        "ts_ns": int(ts.timestamp() * 1e9),
        "strategy_id": "S003",
        "trader_id": "NTX-003",
        "venue": "OKX",
        "is_demo": True,
        "trading_enabled": True,
        "equity_usd": 123.45,
        "unrealized_usd": 0.69,
        "positions_unprotected": 0,
        "instruments": [
            {
                "id": "ETH-USDT-SWAP.OKX",
                "last_close": 2469.05,
                "last_bar_ts_ns": 0,
                "live_bar_age_s": 12.5,
                "bars_seen": 61,
                "warm": True,
            }
        ],
        "positions": [
            {
                "instrument_id": "ETH-USDT-SWAP.OKX",
                "side": "LONG",
                "qty": 0.01,
                "entry_price": 2400.0,
                "mark_price": 2469.05,
                "stop_price": 2350.0,
                "trailing_stop": None,
                "bars_held": 3,
                "unrealized_usd": 0.69,
                "protected": True,
                "trade_id": "t-open",
            }
        ],
        "counters": {
            "live_bars": 10,
            "entries_attempted": 1,
            "entries_submitted": 1,
            "unprotected_events": 0,
            "entries_refused": 0,
            "stops_refused": 0,
            "entry_skips": {},
            "rejects": {},
            "exits": {},
        },
    }
    snap.update(over)
    return snap


def _write_snapshot(data_dir: Path, name: str, snap: dict) -> Path:
    p = data_dir / "state" / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap))
    return p


def _closed(j: Journal, tid: str, inst: str, pnl: float, reason: str, gap: int, fees: float):
    j.open_trade(
        trade_id=tid,
        correlation_id=f"c-{tid}",
        strategy_id="S003",
        venue="OKX",
        instrument_id=inst,
        side="LONG",
        qty=0.01,
        entry_price=2400.0,
        protect_gap_ms=gap,
        is_demo=1,
    )
    j.transition(
        tid,
        "closed",
        exit_price=2450.0,
        exit_reason=reason,
        pnl_net_usd=pnl,
        fees_usd=fees,
        bars_held=4,
    )


def _fill_journal(path: Path) -> Journal:
    j = Journal(path)
    _closed(j, "t1", "ETH-USDT-SWAP.OKX", 2.0, "donchian_exit", 300, 0.02)
    _closed(j, "t2", "ETH-USDT-SWAP.OKX", -1.0, "stop_loss", 500, 0.02)
    _closed(j, "t3", "ADA-USDT-SWAP.OKX", 3.0, "trailing_stop", 100, 0.01)
    j.open_trade(
        trade_id="t-open",
        correlation_id="c-open",
        strategy_id="S003",
        venue="OKX",
        instrument_id="ETH-USDT-SWAP.OKX",
        side="LONG",
        is_demo=1,
    )
    j.record_equity(37.0, 0.0, 0)
    j.record_equity(38.0, 0.5, 1)
    j.event("entry_submitted", severity="info", instrument="ETH-USDT-SWAP.OKX")
    return j


@pytest.fixture()
def dirs(tmp_path):
    data, logs, reports = tmp_path / "data", tmp_path / "logs", tmp_path / "reports"
    for d in (data, logs, reports):
        d.mkdir()
    _write_snapshot(data, "S003-demo", _snapshot(NOW - timedelta(seconds=10)))
    _fill_journal(data / "journal-demo-S003.db")
    Journal(data / "journal.db")  # live S003, empty, no snapshot
    # logs: two valid, one garbage, one blank, one non-object, one with unknown keys
    lines = [
        json.dumps({"timestamp": "2026-09-17T15:59:00Z", "level": "INFO",
                    "component": "S003", "message": "bar received"}),
        "this is not json {",
        "",
        "[1, 2, 3]",
        json.dumps({"timestamp": "2026-09-17T15:59:10Z", "level": "ERROR",
                    "component": "ExecClient", "message": "order rejected", "extra": 1}),
        json.dumps({"message": "no level or timestamp"}),
    ]  # fmt: skip
    (logs / "ntx-S003-demo_2026-09-17.jsonl").write_text("\n".join(lines) + "\n")
    # backtests: two runs, one without metrics
    for name, created, metrics in (
        ("20260916T100000-s003-gate-IS-aaaa", "2026-09-16T10:00:00+00:00", {"sharpe": 0.8}),
        ("20260917T100000-s003-gate-OOS-bbbb", "2026-09-17T10:00:00+00:00", None),
    ):
        d = reports / "runs" / name
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(
            json.dumps(
                {
                    "label": name.split("-", 1)[1],
                    "created_utc": created,
                    "spec": {"timeframe": "4h", "start": "2020-01-01", "end": "2023-12-31",
                             "strategy_cls": "ntx.strategies.donchian_breakout.X"},
                    "environment": {"git_sha": "abc"},
                    "source_hash": {"combined": "965553a6"},
                }
            )
        )  # fmt: skip
        if metrics is not None:
            (d / "metrics.json").write_text(json.dumps(metrics))
    (reports / "runs" / "broken").mkdir()  # no manifest -> skipped
    return data, logs, reports


def _client(dirs, now=NOW) -> TestClient:
    from ntx.ui.app import create_app

    data, logs, reports = dirs
    return TestClient(create_app(data, logs, reports, now=lambda: now))


# ------------------------------------------------------------------ bots / status


def test_bots_lists_snapshot_and_journal_pairs(dirs):
    bots = {b["id"]: b for b in _client(dirs).get("/api/bots").json()["bots"]}
    assert set(bots) == {"S003-demo", "S003-live"}
    demo = bots["S003-demo"]
    assert demo["env"] == "demo" and demo["has_snapshot"] and demo["has_journal"]
    assert demo["stale"] is False and demo["age_s"] == pytest.approx(10, abs=0.01)
    live = bots["S003-live"]
    assert live["env"] == "live" and not live["has_snapshot"] and live["has_journal"]
    assert live["stale"] is True  # no snapshot is never "live" (R22)


def test_journal_file_names_map_to_bot_ids(tmp_path):
    from ntx.ui.sources import journal_bot_id

    assert journal_bot_id("journal.db") == ("S003", "live")
    assert journal_bot_id("journal-demo-S900.db") == ("S900", "demo")
    assert journal_bot_id("journal-S005.db") == ("S005", "live")
    assert journal_bot_id("journal-demo.db") is None  # legacy, ambiguous


def test_status_fresh_snapshot(dirs):
    r = _client(dirs).get("/api/status", params={"bot": "S003-demo"})
    assert r.status_code == 200
    s = r.json()
    assert s["stale"] is False and s["badge"] == "DEMO"
    assert s["equity_usd"] == 123.45 and s["unrealized_usd"] == 0.69
    assert s["positions_unprotected"] == 0
    assert s["counters"]["entries_submitted"] == 1


def test_status_stale_snapshot_is_flagged_r22(dirs):
    s = (
        _client(dirs, now=NOW + timedelta(seconds=200))
        .get("/api/status", params={"bot": "S003-demo"})
        .json()
    )
    assert s["stale"] is True and s["badge"] == "STALE"
    assert s["age_s"] == pytest.approx(210, abs=0.01)


def test_stale_boundary_is_120s(dirs):
    c = _client(dirs, now=NOW + timedelta(seconds=110))  # age 120 -> still fresh
    assert c.get("/api/status", params={"bot": "S003-demo"}).json()["stale"] is False
    c = _client(dirs, now=NOW + timedelta(seconds=111))
    assert c.get("/api/status", params={"bot": "S003-demo"}).json()["stale"] is True


def test_snapshot_with_bad_timestamp_is_stale(dirs):
    data, _, _ = dirs
    snap = _snapshot(NOW)
    snap["ts"] = "garbage"
    _write_snapshot(data, "S003-demo", snap)
    s = _client(dirs).get("/api/status", params={"bot": "S003-demo"}).json()
    assert s["stale"] is True


def test_corrupt_snapshot_is_not_deleted_and_reads_as_missing(dirs):
    data, _, _ = dirs
    p = data / "state" / "S003-demo.json"
    p.write_text("{not json")
    s = _client(dirs).get("/api/status", params={"bot": "S003-demo"}).json()
    assert s["stale"] is True and s["badge"] == "NO SNAPSHOT"
    assert p.exists()  # the UI never writes or deletes the node's files


def test_live_snapshot_badge(dirs):
    data, _, _ = dirs
    _write_snapshot(data, "S003-live", _snapshot(NOW, is_demo=False))
    s = _client(dirs).get("/api/status", params={"bot": "S003-live"}).json()
    assert s["badge"] == "LIVE" and s["env"] == "live"


def test_unknown_bot_is_404_everywhere(dirs):
    c = _client(dirs)
    for ep in ("status", "balance", "positions", "trades", "equity", "stats", "health", "events"):
        assert c.get(f"/api/{ep}", params={"bot": "S999-live"}).status_code == 404, ep


def test_default_bot_when_omitted(dirs):
    s = _client(dirs).get("/api/status").json()
    assert s["bot"] in {"S003-demo", "S003-live"}


# ------------------------------------------------------------------ journal views


def test_positions_come_from_snapshot_with_journal_reference(dirs):
    p = _client(dirs).get("/api/positions", params={"bot": "S003-demo"}).json()
    assert p["stale"] is False
    assert len(p["positions"]) == 1 and p["positions"][0]["protected"] is True
    assert [t["trade_id"] for t in p["journal_open"]] == ["t-open"]


def test_trades_newest_first_with_limit(dirs):
    c = _client(dirs)
    t = c.get("/api/trades", params={"bot": "S003-demo"}).json()["trades"]
    assert [x["trade_id"] for x in t] == ["t3", "t2", "t1"]
    assert t[0]["exit_reason"] == "trailing_stop"
    t = c.get("/api/trades", params={"bot": "S003-demo", "limit": 1}).json()["trades"]
    assert len(t) == 1
    assert c.get("/api/trades", params={"bot": "S003-demo", "limit": 0}).status_code == 422


def test_equity_series(dirs):
    e = _client(dirs).get("/api/equity", params={"bot": "S003-demo"}).json()
    assert [p["equity_usd"] for p in e["points"]] == [37.0, 38.0]
    assert e["hwm"] == 38.0


def test_stats(dirs):
    s = _client(dirs).get("/api/stats", params={"bot": "S003-demo"}).json()
    assert s["closed"] == 3 and s["open"] == 1
    assert s["win_rate"] == pytest.approx(2 / 3)
    assert s["profit_factor"] == pytest.approx(5.0)
    assert s["net_pnl_usd"] == pytest.approx(4.0)
    assert s["fees_usd"] == pytest.approx(0.05)
    assert s["pnl_by_instrument"]["ETH-USDT-SWAP.OKX"] == {"count": 2, "net_usd": 1.0}
    assert s["exit_reasons"] == {"donchian_exit": 1, "stop_loss": 1, "trailing_stop": 1}
    assert s["avg_protect_gap_ms"] == pytest.approx(300)
    assert s["max_protect_gap_ms"] == 500


def test_stats_empty_journal(dirs):
    s = _client(dirs).get("/api/stats", params={"bot": "S003-live"}).json()
    assert s["closed"] == 0 and s["win_rate"] is None and s["profit_factor"] is None


def test_events_newest_first(dirs):
    data, _, _ = dirs
    j = Journal(data / "journal-demo-S003.db")
    j.event("data_stall", severity="critical", instrument="ETH-USDT-SWAP.OKX")
    ev = _client(dirs).get("/api/events", params={"bot": "S003-demo", "limit": 5}).json()
    assert ev["events"][0]["kind"] == "data_stall"
    assert ev["events"][0]["payload"] == {"instrument": "ETH-USDT-SWAP.OKX"}
    assert len(ev["events"]) == 2


# ------------------------------------------------------------------ health


def _checks(c, bot="S003-demo") -> dict:
    h = c.get("/api/health", params={"bot": bot}).json()
    return {x["name"]: x for x in h["checks"]} | {"_overall": h["overall"]}


def test_health_all_green(dirs):
    data, _, _ = dirs
    _write_snapshot(data, "S003-demo", _snapshot(NOW, bar_spec="4-HOUR-LAST-EXTERNAL"))
    ch = _checks(_client(dirs))
    for name in ("snapshot_fresh", "positions_unprotected", "critical_events_24h",
                 "instruments_warm", "bar_age", "journal_vs_snapshot"):  # fmt: skip
        assert ch[name]["status"] == "ok", (name, ch[name])
    assert ch["_overall"] == "ok"


def test_health_bar_age_unknown_without_interval(dirs):
    assert _checks(_client(dirs))["bar_age"]["status"] == "unknown"


def test_health_bar_age_fails_past_two_intervals(dirs):
    data, _, _ = dirs
    snap = _snapshot(NOW, bar_spec="1-HOUR-LAST-EXTERNAL")
    snap["instruments"][0]["live_bar_age_s"] = 2 * 3600 + 1
    _write_snapshot(data, "S003-demo", snap)
    assert _checks(_client(dirs))["bar_age"]["status"] == "fail"


def test_health_unprotected_position_fails(dirs):
    data, _, _ = dirs
    snap = _snapshot(NOW, positions_unprotected=1)
    snap["positions"][0]["protected"] = False
    _write_snapshot(data, "S003-demo", snap)
    ch = _checks(_client(dirs))
    assert ch["positions_unprotected"]["status"] == "fail"
    assert ch["_overall"] == "fail"


def test_health_critical_event_in_24h_fails_and_old_one_does_not(dirs):
    data, _, _ = dirs
    Journal(data / "journal-demo-S003.db").event("data_stall", severity="critical")
    # the event was stamped with the real clock; judge it from 'now' = real now
    real_now = datetime.now(UTC)
    _write_snapshot(data, "S003-demo", _snapshot(real_now))
    assert _checks(_client(dirs, now=real_now))["critical_events_24h"]["status"] == "fail"
    later = real_now + timedelta(hours=25)
    assert _checks(_client(dirs, now=later))["critical_events_24h"]["status"] == "ok"


def test_health_cold_instrument_warns(dirs):
    data, _, _ = dirs
    snap = _snapshot(NOW)
    snap["instruments"][0]["warm"] = False
    _write_snapshot(data, "S003-demo", snap)
    assert _checks(_client(dirs))["instruments_warm"]["status"] == "warn"


def test_health_journal_mismatch_is_warning_not_truth_r27(dirs):
    data, _, _ = dirs
    _write_snapshot(data, "S003-demo", _snapshot(NOW, positions=[]))
    ch = _checks(_client(dirs))
    assert ch["journal_vs_snapshot"]["status"] == "warn"
    assert "R27" in ch["journal_vs_snapshot"]["detail"]


def test_health_stale_snapshot_fails_and_hides_derived_checks(dirs):
    ch = _checks(_client(dirs, now=NOW + timedelta(hours=1)))
    assert ch["snapshot_fresh"]["status"] == "fail"
    # a stale snapshot cannot vouch for anything live (R22)
    for name in ("positions_unprotected", "instruments_warm", "bar_age", "journal_vs_snapshot"):
        assert ch[name]["status"] == "unknown", name
    assert ch["_overall"] == "fail"


# ------------------------------------------------------------------ logs / backtests


def test_logs_skip_malformed_lines(dirs):
    lines = _client(dirs).get("/api/logs").json()["lines"]
    assert [x["message"] for x in lines] == [
        "bar received",
        "order rejected",
        "no level or timestamp",
    ]


def test_logs_level_filter_is_a_minimum(dirs):
    c = _client(dirs)
    lines = c.get("/api/logs", params={"level": "WARNING"}).json()["lines"]
    assert [x["message"] for x in lines] == ["order rejected"]
    assert c.get("/api/logs", params={"level": "BOGUS"}).status_code == 422


def test_logs_tail_n_and_bot_filter(dirs):
    _, logs, _ = dirs
    (logs / "ntx-S900-demo_2026-09-17.jsonl").write_text(
        json.dumps({"level": "INFO", "message": "other bot"}) + "\n"
    )
    c = _client(dirs)
    tail = c.get("/api/logs", params={"n": 1, "bot": "S003-demo"}).json()["lines"]
    assert [x["message"] for x in tail] == ["no level or timestamp"]
    assert len(c.get("/api/logs").json()["lines"]) == 4
    only = c.get("/api/logs", params={"bot": "S900-demo"}).json()["lines"]
    assert [x["message"] for x in only] == ["other bot"]


def test_logs_empty_dir(tmp_path):
    from ntx.ui.app import create_app

    c = TestClient(create_app(tmp_path / "d", tmp_path / "l", tmp_path / "r", now=lambda: NOW))
    assert c.get("/api/logs").json()["lines"] == []
    assert c.get("/api/backtests").json()["runs"] == []
    # the live bot is listed before it has ever run -- otherwise there is no way
    # to select it and read what the live account holds
    assert [b["id"] for b in c.get("/api/bots").json()["bots"]] == ["S003-live"]
    s = c.get("/api/status").json()
    assert s["bot"] == "S003-live" and s["badge"] == "NO SNAPSHOT"


def test_backtests_newest_first(dirs):
    runs = _client(dirs).get("/api/backtests").json()["runs"]
    assert [r["id"] for r in runs] == [
        "20260917T100000-s003-gate-OOS-bbbb",
        "20260916T100000-s003-gate-IS-aaaa",
    ]
    assert runs[0]["metrics"] is None
    assert runs[1]["metrics"] == {"sharpe": 0.8}
    assert runs[1]["timeframe"] == "4h" and runs[1]["source_hash"] == "965553a6"


# ------------------------------------------------------------------ safety


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_ui_never_writes_the_journal(dirs):
    data, _, _ = dirs
    db = data / "journal-demo-S003.db"
    before, mtime = _digest(db), db.stat().st_mtime_ns
    c = _client(dirs)
    for ep in ("status", "positions", "trades", "equity", "stats", "health", "events"):
        assert c.get(f"/api/{ep}", params={"bot": "S003-demo"}).status_code == 200
    assert _digest(db) == before and db.stat().st_mtime_ns == mtime


def test_sqlite_is_opened_with_mode_ro(dirs, monkeypatch):
    from ntx.ui import sources

    seen = []
    real = sqlite3.connect

    def spy(database, *a, **kw):
        seen.append((str(database), kw.get("uri")))
        return real(database, *a, **kw)

    monkeypatch.setattr(sources.sqlite3, "connect", spy)
    _client(dirs).get("/api/stats", params={"bot": "S003-demo"})
    assert seen, "journal was never opened"
    assert all(db.startswith("file:") and "mode=ro" in db and uri for db, uri in seen)


def test_readonly_connection_refuses_writes(dirs):
    from ntx.ui.sources import connect_ro

    data, _, _ = dirs
    with connect_ro(data / "journal-demo-S003.db") as c, pytest.raises(sqlite3.OperationalError):
        c.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")


def test_missing_journal_is_not_created(tmp_path):
    from ntx.ui.app import create_app

    data = tmp_path / "data"
    _write_snapshot(data, "S900-demo", _snapshot(NOW, strategy_id="S900"))
    c = TestClient(create_app(data, tmp_path / "l", tmp_path / "r", now=lambda: NOW))
    assert c.get("/api/trades", params={"bot": "S900-demo"}).json()["trades"] == []
    assert c.get("/api/health", params={"bot": "S900-demo"}).status_code == 200
    assert not (data / "journal-demo-S900.db").exists()


def test_only_get_routes_exist(dirs):
    c = _client(dirs)
    for route in c.app.routes:
        methods = getattr(route, "methods", None) or set()
        assert methods <= {"GET", "HEAD"}, (route.path, methods)
    assert c.post("/api/status").status_code == 405


def test_no_external_docs_pages(dirs):
    # swagger/redoc pull JS from a CDN; the monitor has no internet need
    c = _client(dirs)
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "localhost", ""])
def test_host_guard_refuses_non_loopback(host, monkeypatch):
    from ntx.ui import __main__ as m

    called = []
    monkeypatch.setattr(m.uvicorn, "run", lambda *a, **k: called.append(k))
    with pytest.raises(SystemExit) as e:
        m.main(["--host", host])
    assert e.value.code != 0 and not called


def test_main_binds_loopback(monkeypatch, tmp_path):
    from ntx.ui import __main__ as m

    called = []
    monkeypatch.setattr(m.uvicorn, "run", lambda app, **k: called.append(k))
    assert m.main(["--port", "8799", "--data-dir", str(tmp_path)]) == 0
    assert called[0]["host"] == "127.0.0.1" and called[0]["port"] == 8799


# ------------------------------------------------------------------ static page


def test_index_and_assets_are_served_locally(dirs):
    c = _client(dirs)
    html = c.get("/").text
    assert "app.js" in html and "app.css" in html
    assert "http://" not in html and "https://" not in html  # no CDN
    js = c.get("/static/app.js")
    css = c.get("/static/app.css")
    assert js.status_code == 200 and css.status_code == 200
    assert "https://" not in js.text and "http://" not in js.text
    assert "prefers-color-scheme" in css.text


def test_page_renders_stale_badge_r22(dirs):
    js = _client(dirs).get("/static/app.js").text
    # the badge text comes from the API's `badge`, and stale data is visibly marked
    assert "status.badge" in js and "stale" in js
    assert "STALE" in js


def test_status_passes_both_equity_figures_to_the_page(tmp_path):
    """So the page can say 'demo account holds $5,012 -- sized as if $123.45'."""
    import inspect

    from ntx.ui import app as ui

    src = inspect.getsource(ui)
    assert '"account_equity_usd"' in src and '"equity_override_usd"' in src


# ------------------------------------------------------------------ the two equity figures


def _demo_with_override(data: Path) -> None:
    """The demo soak: play money in the account, sized as if it were the live one."""
    _write_snapshot(
        data,
        "S003-demo",
        _snapshot(
            NOW - timedelta(seconds=10),
            equity_usd=1000.0,
            sizing_equity_usd=1000.0,
            account_equity_usd=4999.66,
            equity_override_usd=1000.0,
            account_label="nautilus-demo",
        ),
    )


def test_status_labels_account_balance_and_sizing_basis_distinctly(dirs):
    """The owner read the sizing basis as their balance once; the two must not look alike."""
    data, _, _ = dirs
    _demo_with_override(data)
    e = _client(dirs).get("/api/status", params={"bot": "S003-demo"}).json()["equity"]
    assert e["account"]["usd"] == 4999.66
    assert e["sizing"]["usd"] == 1000.0
    assert e["account"]["label"] and e["sizing"]["label"]
    assert e["account"]["label"] != e["sizing"]["label"]
    assert e["sizing"]["is_override"] is True
    assert e["account"]["account_label"] == "nautilus-demo"
    # the gloss is a sentence on the page, not a tooltip
    assert "NTX_EQUITY_OVERRIDE_USD" in e["note"]


def test_status_sizing_prefers_the_snapshots_own_key_over_the_legacy_alias(dirs):
    data, _, _ = dirs
    _write_snapshot(
        data,
        "S003-demo",
        _snapshot(NOW - timedelta(seconds=10), equity_usd=9.99, sizing_equity_usd=1000.0),
    )
    e = _client(dirs).get("/api/status", params={"bot": "S003-demo"}).json()["equity"]
    assert e["sizing"]["usd"] == 1000.0


def test_status_equity_says_nothing_is_sized_when_no_node_is_running(dirs):
    """The live account before go-live: real money, no snapshot, nothing sized from it."""
    e = _client(dirs).get("/api/status", params={"bot": "S003-live"}).json()["equity"]
    assert e["sizing"]["usd"] is None and e["account"]["usd"] is None
    assert "No node is running" in e["note"]


def test_status_equity_without_an_override_says_both_figures_are_the_same(dirs):
    e = _client(dirs).get("/api/status", params={"bot": "S003-demo"}).json()["equity"]
    assert e["sizing"]["is_override"] is False
    assert e["sizing"]["usd"] == 123.45
    assert "same" in e["note"]


# ------------------------------------------------------------------ venue balance


def _balance(usd=100.25, as_of="2026-09-24T09:00:00+00:00", error=None):
    from ntx.ui.sources import Balance

    return Balance(usd=usd, as_of=as_of, source="venue", error=error)


def _client_with_balance(data: Path, tmp_path: Path, fetch, **kw) -> TestClient:
    from ntx.ui.app import create_app
    from ntx.ui.sources import VenueBalance

    return TestClient(
        create_app(
            data,
            tmp_path / "logs",
            tmp_path / "reports",
            now=lambda: NOW,
            venue_balance=VenueBalance(fetch, **kw),
        )
    )


def test_live_bot_with_no_snapshot_and_no_journal_still_reports_a_balance(tmp_path):
    """The live node has never run: no snapshot, no journal, no trades -- still a balance."""
    data = tmp_path / "data"
    data.mkdir()
    calls: list[str] = []

    def fetch(env: str):
        calls.append(env)
        return _balance()

    c = _client_with_balance(data, tmp_path, fetch)
    assert "S003-live" in {b["id"] for b in c.get("/api/bots").json()["bots"]}
    b = c.get("/api/balance", params={"bot": "S003-live"}).json()
    assert b["usd"] == 100.25
    assert b["source"] == "venue"
    assert b["error"] is None
    assert b["ccy"] == "USDT"
    assert b["as_of"] == "2026-09-24T09:00:00+00:00"
    assert calls == ["live"]


def test_balance_prefers_a_fresh_snapshot_over_the_venue(dirs, tmp_path):
    data, _, _ = dirs
    _demo_with_override(data)
    calls: list[str] = []

    c = _client_with_balance(data, tmp_path, lambda env: calls.append(env) or _balance())
    b = c.get("/api/balance", params={"bot": "S003-demo"}).json()
    assert b["usd"] == 4999.66 and b["source"] == "snapshot" and calls == []


def test_balance_falls_back_to_the_venue_when_the_snapshot_is_stale(dirs, tmp_path):
    data, _, _ = dirs
    _write_snapshot(data, "S003-demo", _snapshot(NOW - timedelta(seconds=600)))
    c = _client_with_balance(data, tmp_path, lambda env: _balance(usd=4999.66))
    b = c.get("/api/balance", params={"bot": "S003-demo"}).json()
    assert b["source"] == "venue" and b["usd"] == 4999.66


def test_balance_says_why_it_is_unavailable_instead_of_showing_zero(tmp_path):
    data = tmp_path / "data"
    data.mkdir()

    def fetch(env: str):
        raise OSError("connection refused")

    b = (
        _client_with_balance(data, tmp_path, fetch)
        .get("/api/balance", params={"bot": "S003-live"})
        .json()
    )
    assert b["usd"] is None
    assert b["error"].startswith("balance unavailable:")
    assert "connection refused" in b["error"]


def test_balance_does_not_block_the_page_when_the_venue_hangs(tmp_path):
    import time

    data = tmp_path / "data"
    data.mkdir()

    def fetch(env: str):
        time.sleep(5)
        return _balance()

    c = _client_with_balance(data, tmp_path, fetch, wait_s=0.2)
    t0 = time.monotonic()
    b = c.get("/api/balance", params={"bot": "S003-live"}).json()
    assert time.monotonic() - t0 < 2.0
    assert b["usd"] is None and b["error"].startswith("balance unavailable:")


def test_balance_is_cached_for_at_least_30s(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    calls: list[str] = []
    clock = [1000.0]

    c = _client_with_balance(
        data,
        tmp_path,
        lambda env: calls.append(env) or _balance(),
        clock=lambda: clock[0],
    )
    for _ in range(3):
        assert c.get("/api/balance", params={"bot": "S003-live"}).json()["usd"] == 100.25
    assert calls == ["live"]
    clock[0] += 31.0
    assert c.get("/api/balance", params={"bot": "S003-live"}).json()["usd"] == 100.25
    assert calls == ["live", "live"]


def test_venue_fetch_keeps_credentials_out_of_the_payload(monkeypatch, tmp_path):
    """R1: the browser must never see key material, not even in an error string."""
    from ntx.ui import sources

    seen = {}

    def fake_signed(method, base, path, creds, demo, body=None):
        seen["creds"], seen["demo"], seen["path"] = creds, demo, path
        return {"code": "0", "data": [{"details": [{"ccy": "USDT", "eq": "100.25"}]}]}

    monkeypatch.setattr(sources, "_signed", fake_signed)
    monkeypatch.setattr(
        sources,
        "_live_creds",
        lambda env: ("KEY-abc", "SECRET-def", "PASS-ghi", "https://www.okx.com"),
    )
    bal = sources.fetch_venue_usdt("live")
    assert bal.usd == 100.25 and seen["path"] == "/api/v5/account/balance"
    blob = json.dumps({"usd": bal.usd, "as_of": bal.as_of, "error": bal.error, "src": bal.source})
    for secret in ("KEY-abc", "SECRET-def", "PASS-ghi"):
        assert secret not in blob


def test_usdt_equity_ignores_coin_dust(tmp_path):
    from ntx.ui.sources import usdt_equity

    body = {"data": [{"details": [{"ccy": "BTC", "eq": "0.5"}, {"ccy": "USDT", "eq": "99.5"}]}]}
    assert usdt_equity(body) == 99.5
    assert usdt_equity({"data": []}) is None
    assert usdt_equity({"data": [{"details": [{"ccy": "BTC", "eq": "0.5"}]}]}) == 0.0


def test_page_shows_the_account_balance_and_the_sizing_basis_separately(dirs):
    c = _client(dirs)
    html = c.get("/").text
    js = c.get("/static/app.js").text
    for el in ("k-account", "k-sizing", "equity-gloss"):
        assert f'id="{el}"' in html, el
    assert "api/balance" in js or '"balance"' in js
    assert "k-account" in js and "k-sizing" in js and "equity-gloss" in js


# ------------------------------------------------ PnL as a percentage ------ #
def test_stats_carry_the_sizing_basis_so_pnl_can_be_shown_as_a_percentage(dirs):
    """A dollar figure means nothing without the capital behind it.

    The denominator has to be the SIZING BASIS -- what trades are actually sized
    from -- not the account balance. On the demo bots those differ by 5x: the
    account holds ~$5,000 while the strategy sizes from $1,000. Dividing by the
    balance would make identical strategy performance look five times worse on
    demo than on live, which is exactly backwards for the thing the owner wants
    it for: comparing bots and showing them to other people.
    """
    s = _client(dirs).get("/api/stats", params={"bot": "S003-demo"}).json()
    assert "sizing_equity_usd" in s, "no denominator, so the UI cannot compute a percentage"
    assert s["sizing_equity_usd"] == pytest.approx(123.45)


def test_net_pnl_percentage_is_of_the_sizing_basis(dirs):
    s = _client(dirs).get("/api/stats", params={"bot": "S003-demo"}).json()
    assert s["net_pnl_pct"] == pytest.approx(4.0 / 123.45 * 100)


def test_status_carries_unrealized_as_a_percentage_too(dirs):
    st = _client(dirs).get("/api/status", params={"bot": "S003-demo"}).json()
    assert "unrealized_pct" in st
    if st.get("unrealized_usd") is not None and st.get("equity_usd"):
        assert st["unrealized_pct"] == pytest.approx(st["unrealized_usd"] / st["equity_usd"] * 100)


def test_no_percentage_without_a_basis_rather_than_a_wrong_one(tmp_path):
    """A missing or zero basis must give null, never a divide-by-zero or a
    silently-wrong figure presented to someone being shown the dashboard."""
    from ntx.ui.app import pct_of

    assert pct_of(4.0, 0) is None
    assert pct_of(4.0, None) is None
    assert pct_of(None, 100.0) is None
    assert pct_of(4.0, 100.0) == pytest.approx(4.0)
    assert pct_of(-2.5, 50.0) == pytest.approx(-5.0)


def test_the_page_renders_the_percentage_next_to_the_dollar_figure():
    """The API can supply it and the page must use it, or the feature is invisible."""
    from pathlib import Path

    js = Path("src/ntx/ui/static/app.js").read_text()
    assert "signedWithPct" in js
    assert "status.unrealized_pct" in js and "s.net_pnl_pct" in js


def test_the_label_says_what_the_percentage_is_of():
    """A bare '%' invites the reader to assume it is of their account balance."""
    from pathlib import Path

    html = Path("src/ntx/ui/static/index.html").read_text()
    assert html.count("% of sizing basis") >= 2


def test_static_assets_are_never_cached(dirs):
    """A deployed UI change that the browser does not show is worse than no change.

    Measured 2026-09-26: the PnL percentage shipped, the API served it, and the
    owner's browser rendered the NEW index.html (the label changed) while running
    the OLD app.js from cache -- so the feature appeared broken. The dashboard is
    read from one laptop over an SSH tunnel; there is nothing to gain from caching
    it and a whole class of "I deployed but cannot see it" to lose.
    """
    r = _client(dirs).get("/static/app.js")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "no-cache" in cc or "no-store" in cc, f"cache-control was {cc!r}"


def test_the_page_itself_is_not_cached_either(dirs):
    r = _client(dirs).get("/")
    cc = r.headers.get("cache-control", "")
    assert "no-cache" in cc or "no-store" in cc, f"cache-control was {cc!r}"
