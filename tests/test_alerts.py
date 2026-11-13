"""Alerting + dead-man switch (docs/23 §4). Written before the code (R41).

Two separate jobs:
  * the ALERTER says what went wrong (journal events, stale snapshot, dead service);
  * the DEAD-MAN SWITCH says "I am still alive" to an outside service, because an
    alarm inside the bot cannot report the bot's own death.
"""

from __future__ import annotations

import json
import time

import pytest

from ntx.obs.alerts import (
    CURSOR_NAME,
    Alert,
    build_alerts,
    collect,
    load_cursor,
    redact,
    save_cursor,
)
from ntx.obs.journal import Journal


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal-demo-S003.db")


def _snapshot(tmp_path, age_s=5.0, **over):
    import time

    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    snap = {
        "schema": 1,
        "ts_ns": int((time.time() - age_s) * 1e9),
        "strategy_id": "S003",
        "is_demo": True,
        "positions_unprotected": 0,
        "equity_usd": 123.45,
        "positions": [],
        "instruments": [],
        "counters": {},
    }
    snap.update(over)
    p = d / "S003-demo.json"
    p.write_text(json.dumps(snap))
    return p


# --------------------------------------------------------------- cursor ---- #
def test_cursor_round_trip(tmp_path):
    assert load_cursor(tmp_path, "S003-demo") == 0
    save_cursor(tmp_path, "S003-demo", 42)
    assert load_cursor(tmp_path, "S003-demo") == 42
    assert (tmp_path / CURSOR_NAME).exists()


def test_each_event_is_alerted_once(journal, tmp_path):
    """`events.seq` is a monotonic cursor: no time windows, so a clock change
    can neither duplicate nor skip an alert."""
    journal.event("stop_refused", severity="critical", instrument="SOL")
    first = collect(journal, since_seq=0)
    assert [a.kind for a in first] == ["stop_refused"]
    assert collect(journal, since_seq=first[-1].seq) == []
    journal.event("guard_kill", severity="critical", reason="drawdown 21% >= 20%")
    again = collect(journal, since_seq=first[-1].seq)
    assert [a.kind for a in again] == ["guard_kill"]


def test_only_warning_and_critical_are_alerted(journal):
    journal.event("entry_rejected", severity="info", instrument="SOL")
    journal.event("warmup_short", severity="warning", instrument="SOL")
    journal.event("position_unprotected", severity="critical", instrument="SOL")
    got = collect(journal, since_seq=0)
    assert [a.severity for a in got] == ["warning", "critical"]


# ------------------------------------------------------------ snapshot ----- #
def test_stale_snapshot_raises_a_critical_alert(tmp_path):
    _snapshot(tmp_path, age_s=600)
    alerts = build_alerts(tmp_path, max_age_s=120)
    assert any(a.kind == "snapshot_stale" and a.severity == "critical" for a in alerts)


def test_fresh_snapshot_is_quiet(tmp_path):
    _snapshot(tmp_path, age_s=5)
    assert build_alerts(tmp_path, max_age_s=120) == []


def test_unprotected_position_is_critical_even_without_an_event(tmp_path):
    """Belt and braces: the snapshot alone must raise R15."""
    _snapshot(tmp_path, positions_unprotected=2)
    alerts = build_alerts(tmp_path, max_age_s=120)
    assert any(a.kind == "positions_unprotected" and a.severity == "critical" for a in alerts)


def test_missing_snapshot_is_reported(tmp_path):
    (tmp_path / "state").mkdir()
    alerts = build_alerts(tmp_path, max_age_s=120, expect=["S003-demo"])
    assert any(a.kind == "snapshot_missing" for a in alerts)


def test_journal_events_reach_build_alerts(tmp_path, journal):
    _snapshot(tmp_path, age_s=5)
    journal.event("close_refused", severity="critical", instrument="ETH", reason="rejected")
    alerts = build_alerts(tmp_path, max_age_s=120, journals=[journal.path])
    assert any(a.kind == "close_refused" for a in alerts)


# ------------------------------------------------------------- privacy ---- #
def test_alert_text_never_carries_money_or_secrets():
    """A push topic is a semi-public channel (docs/23 §7): the decision travels,
    the account details do not."""
    a = Alert(
        kind="guard_kill",
        severity="critical",
        text="drawdown 21.0% >= 20.0%",
        extra={
            "equity_usd": 123.45,
            "pnl_net_usd": -3.2,
            "qty": 0.55,
            "api_key": "abc",
            "instrument": "SOL-USDT-SWAP.OKX",
            "reason": "drawdown 21.0% >= 20.0%",
        },
    )
    text = redact(a)
    assert "SOL-USDT-SWAP.OKX" in text and "drawdown 21.0%" in text
    for secret in ("123.45", "-3.2", "0.55", "abc", "api_key", "equity_usd", "pnl"):
        assert secret not in text, secret


def test_redact_keeps_the_kind_and_severity():
    text = redact(Alert(kind="data_stall", severity="critical", text="no bars", extra={}))
    assert "data_stall" in text and "critical" in text.lower()


# ----------------------------------------------------------- delivery ----- #
def test_send_posts_to_every_configured_sink(monkeypatch):
    from ntx.obs import alerts as mod

    posted = []
    monkeypatch.setattr(mod, "_post", lambda url, data, headers=None: posted.append((url, data)))
    n = mod.send(
        [Alert(kind="x", severity="critical", text="t", extra={})],
        ntfy_url="https://ntfy.sh/ntx-secret-topic",
    )
    assert n == 1 and posted and "ntfy.sh" in posted[0][0]


def test_send_without_a_sink_is_a_no_op_not_a_crash(monkeypatch):
    from ntx.obs import alerts as mod

    monkeypatch.setattr(mod, "_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    assert mod.send([Alert(kind="x", severity="critical", text="t", extra={})]) == 0


def test_a_failing_sink_never_raises(monkeypatch):
    from ntx.obs import alerts as mod

    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(mod, "_post", boom)
    assert mod.send([Alert("x", "critical", "t", {})], ntfy_url="https://ntfy.sh/t") == 0


# ------------------------------------------------------- dead-man switch --- #
def test_heartbeat_pings_only_when_healthy(monkeypatch):
    from ntx.obs import alerts as mod

    pings = []
    monkeypatch.setattr(mod, "_post", lambda url, data, headers=None: pings.append(url))
    mod.heartbeat("https://hc-ping.com/uuid", healthy=True)
    assert pings == ["https://hc-ping.com/uuid"]
    mod.heartbeat("https://hc-ping.com/uuid", healthy=False)
    assert pings[-1].endswith("/fail"), "an unhealthy run must report failure, not silence"


def test_heartbeat_without_a_url_does_nothing(monkeypatch):
    from ntx.obs import alerts as mod

    monkeypatch.setattr(mod, "_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    mod.heartbeat(None, healthy=True)


# ------------------------------------------------- correlation in the logs -- #
def test_entry_and_exit_log_lines_carry_the_trade_id():
    """docs/23 §5: following one trade end to end needs the id IN the log line,
    not only in the journal."""
    import inspect

    from ntx.strategies.base import NtxStrategy

    entry = inspect.getsource(NtxStrategy._enter)
    assert "corr=" in entry and "trade=" in entry
    assert "corr=" in inspect.getsource(NtxStrategy._close_confirmed)


def test_dry_run_does_not_consume_alerts(tmp_path, journal):
    """A dry run must never swallow an alert: the cursor advances only once the
    alerts have actually been handed to the sinks."""
    _snapshot(tmp_path, age_s=5)
    journal.event("guard_kill", severity="critical", reason="drawdown")
    first = build_alerts(tmp_path, journals=[journal.path])
    assert [a.kind for a in first] == ["guard_kill"]
    again = build_alerts(tmp_path, journals=[journal.path])
    assert [a.kind for a in again] == ["guard_kill"], "not delivered yet, must still be pending"

    from ntx.obs.alerts import advance_cursors

    advance_cursors(tmp_path, again)
    assert build_alerts(tmp_path, journals=[journal.path]) == []


def test_retired_bot_snapshots_do_not_nag_forever(tmp_path):
    """A rehearsal bot that finished days ago is not an incident."""
    _snapshot(tmp_path, age_s=5)
    old = tmp_path / "state" / "S900-demo.json"
    old.write_text(json.dumps({"schema": 1, "ts_ns": int((time.time() - 3 * 86400) * 1e9)}))
    assert build_alerts(tmp_path, max_age_s=120) == []


def test_expected_bots_are_alerted_however_old(tmp_path):
    """But a bot we declared must be running is an incident, at any age."""
    old = tmp_path / "state"
    old.mkdir(exist_ok=True)
    (old / "S003-demo.json").write_text(
        json.dumps({"schema": 1, "ts_ns": int((time.time() - 3 * 86400) * 1e9)})
    )
    alerts = build_alerts(tmp_path, max_age_s=120, expect=["S003-demo"])
    assert any(a.kind == "snapshot_stale" for a in alerts)


# ------------------------------------------------------------------ F053 --- #
def test_a_deploy_does_not_page_the_operator(tmp_path):
    """Every deploy stops the node for ~2-3 minutes to swap the code, which is
    longer than the 120 s staleness limit -- so each one fired `snapshot_stale`,
    pinged the dead-man switch `/fail`, and paged the operator for a healthy,
    intentional restart. Two of those landed in the operator's inbox on
    2026-09-20 before anyone noticed the pattern.

    An alert you get for routine work is how real ones stop being read.
    """
    from ntx.obs.alerts import MAINTENANCE_NAME, build_alerts

    _snapshot(tmp_path, age_s=600)
    assert any(a.kind == "snapshot_stale" for a in build_alerts(tmp_path, max_age_s=120))

    (tmp_path / MAINTENANCE_NAME).write_text(str(time.time()))
    assert build_alerts(tmp_path, max_age_s=120) == [], "a deploy in progress is not an incident"


def test_a_position_left_unprotected_still_alerts_during_a_deploy(tmp_path):
    """Only staleness is excused. R15 is never excused."""
    from ntx.obs.alerts import MAINTENANCE_NAME, build_alerts

    _snapshot(tmp_path, age_s=600, positions_unprotected=1)
    (tmp_path / MAINTENANCE_NAME).write_text(str(time.time()))
    kinds = [a.kind for a in build_alerts(tmp_path, max_age_s=120)]
    assert "positions_unprotected" in kinds
    assert "snapshot_stale" not in kinds


def test_a_deploy_marker_left_behind_stops_excusing_anything(tmp_path):
    """A deploy that died mid-way must not silence the alerter forever."""
    from ntx.obs.alerts import MAINTENANCE_MAX_S, MAINTENANCE_NAME, build_alerts

    _snapshot(tmp_path, age_s=600)
    (tmp_path / MAINTENANCE_NAME).write_text(str(time.time() - MAINTENANCE_MAX_S - 60))
    assert any(a.kind == "snapshot_stale" for a in build_alerts(tmp_path, max_age_s=120))


def test_an_unreadable_deploy_marker_is_ignored_not_trusted(tmp_path):
    from ntx.obs.alerts import MAINTENANCE_NAME, build_alerts

    _snapshot(tmp_path, age_s=600)
    (tmp_path / MAINTENANCE_NAME).write_text("not a timestamp")
    assert any(a.kind == "snapshot_stale" for a in build_alerts(tmp_path, max_age_s=120))


# ------------------------------------------------------------ O2-02, O2-01 -- #
def test_a_burst_of_alerts_does_not_truncate_the_important_one(tmp_path, monkeypatch, journal):
    """Reproduced by the round-2 review: 80 warnings plus one `close_refused`
    ("MANUAL ACTION NEEDED") produced a 3,500-character body holding 24 of the
    81 lines -- the critical was cut off -- while the cursor advanced past all
    81. The alert that says a position could not be closed was the one lost."""
    from ntx.obs import alerts as mod

    posted: list[str] = []
    monkeypatch.setattr(mod, "_post", lambda url, data, headers=None: posted.append(data.decode()))
    _snapshot(tmp_path, age_s=5)
    for i in range(80):
        journal.event("entry_rejected", severity="warning", instrument=f"X{i}", reason="below min")
    journal.event("close_refused", severity="critical", instrument="SOL", reason="MANUAL ACTION")

    alerts = build_alerts(tmp_path, journals=[journal.path])
    sent = mod.send(alerts, ntfy_url="https://ntfy.sh/t")
    assert sent == len(alerts), "every alert must be delivered, not the first 3,500 characters"
    assert any("close_refused" in body for body in posted), "the critical never reached a sink"


def test_the_cursor_only_passes_what_was_actually_delivered(tmp_path, monkeypatch, journal):
    """If the third chunk fails, the events in it must still be pending."""
    from ntx.obs import alerts as mod

    calls = {"n": 0}

    def flaky(url, data, headers=None):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("ntfy 429")

    monkeypatch.setattr(mod, "_post", flaky)
    _snapshot(tmp_path, age_s=5)
    for i in range(120):
        journal.event("entry_rejected", severity="warning", instrument=f"X{i}", reason="r")

    alerts = build_alerts(tmp_path, journals=[journal.path])
    sent = mod.send(alerts, ntfy_url="https://ntfy.sh/t")
    assert 0 < sent < len(alerts), sent
    mod.advance_cursors(tmp_path, alerts[:sent])
    still = build_alerts(tmp_path, journals=[journal.path])
    assert len(still) == len(alerts) - sent, "undelivered events must stay pending"


def test_the_deployer_cannot_alert_on_every_timer_tick():
    """O2-01: with the live node running, update.sh reported the pending commit
    on every tick -- 1,440 pushes a day into a free-tier topic, which is how a
    real alert becomes undeliverable. It was rate-limited with a marker file, and
    then the reason for it disappeared: since the two-checkout split (docs/24
    section 2) a commit never waits, so there is nothing to announce. What must
    stay true is the shape -- nothing on the every-minute path may send."""
    from pathlib import Path

    update = (Path(__file__).resolve().parents[1] / "deploy" / "update.sh").read_text()
    assert ".deploy-announced" not in update, "commits no longer wait; nothing to announce"
    # Everything between the fetch and the "there is a new commit" branch runs
    # 1,440 times a day. Nothing there may send.
    tick = update[update.index("git fetch --quiet origin") : update.index('log "updating')]
    assert 'alert "' not in tick, "an alert on the every-minute path"
    # The one alert reachable from that branch is the unit-file drift warning,
    # and it sits behind the stamp: at most once every NTX_DEPLOY_VERIFY_EVERY.
    assert tick.index("stamp_is_fresh") < tick.index("check_unit_drift")
