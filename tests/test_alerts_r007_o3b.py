"""R-007 Part 5, the findings left after O3-01..03 (d87a344).

O3-04 / O2-05  One malformed snapshot (`ts_ns: null`, `positions: ["x"]`, a string
               count) raised out of `build_alerts` and took the WHOLE alerter down:
               no push for any bot, only a bare dead-man DOWN much later.
O3-06          A latched KILL was in the snapshot and the alerter never read it.
O2-04 / O3-07  A deploy in progress excused staleness in the alerts but not in the
               dead-man switch, which pinged `/fail` for routine work; one shared
               marker let a demo deploy excuse the LIVE bot; and `--message` runs
               pinged healthy, masking a hung timer.
O2-02          A recreated journal restarts at seq 1 while the cursor still says
               500: every new event was below the cursor and never read.
O3-09          Dollar amounts reached the push topic through alert TEXT.
O3-10          The alerter deleted a bot's corrupt snapshot (and its own cursor).

Written before the fixes (R41).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from ntx.obs import alerts as mod
from ntx.obs.alerts import MAINTENANCE_NAME, Alert, build_alerts, collect, redact
from ntx.obs.journal import Journal


def _snap(tmp_path, bot="S003-demo", age_s=5.0, **over):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    snap = {
        "schema": 1,
        "ts_ns": int((time.time() - age_s) * 1e9),
        "strategy_id": bot.split("-")[0],
        "is_demo": not bot.endswith("-live"),
        "positions_unprotected": 0,
        "positions": [],
    }
    snap.update(over)
    p = d / f"{bot}.json"
    p.write_text(json.dumps(snap))
    return p


def _settings(monkeypatch, tmp_path, bots="S003-demo", hc="https://hc-ping.com/u"):
    s = SimpleNamespace(
        data_dir=tmp_path,
        alert_ntfy_url="https://ntfy.sh/t",
        healthcheck_url=hc,
        alert_bots=bots,
        alert_fills=False,
    )
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)


def _record_posts(monkeypatch) -> list:
    posts: list = []
    monkeypatch.setattr(
        mod, "_post", lambda url, data, headers=None: posts.append((url, data or b""))
    )
    return posts


# ------------------------------------------------------------------ O3-04 -- #
def test_a_malformed_snapshot_is_reported_and_does_not_hide_the_others(tmp_path):
    _snap(tmp_path, "S003-live", ts_ns=None)
    _snap(tmp_path, "S004-demo", positions=["x"])
    _snap(tmp_path, "S900-demo", positions_unprotected="n/a")
    _snap(tmp_path, "S003-demo", positions_unprotected=1)
    alerts = build_alerts(tmp_path, max_age_s=120)
    unreadable = {a.extra.get("bot") for a in alerts if a.kind == "snapshot_unreadable"}
    assert unreadable == {"S003-live", "S004-demo", "S900-demo"}
    assert any(
        a.kind == "positions_unprotected" and a.extra.get("bot") == "S003-demo" for a in alerts
    ), "one bad file must not silence a real incident on another bot"


def test_fresh_bots_survives_a_null_timestamp(tmp_path):
    _snap(tmp_path, "S003-live", ts_ns=None)
    assert mod.fresh_bots(tmp_path, ["S003-live"]) == []


def test_an_unexpected_error_still_pushes_and_fails_the_dead_man(tmp_path, monkeypatch):
    _snap(tmp_path)
    _settings(monkeypatch, tmp_path)
    posts = _record_posts(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("schema drift")

    monkeypatch.setattr(mod, "build_alerts", boom)
    assert mod.main([]) == 1
    assert any(b"alerter_crashed" in data for _url, data in posts), "the crash was not pushed"
    assert any(url.endswith("/fail") for url, _ in posts), "the dead-man was not told"


# ------------------------------------------------------------------ O3-06 -- #
def test_a_latched_kill_is_pushed(tmp_path):
    _snap(tmp_path, "S003-live", killed=True)
    kinds = [a.kind for a in build_alerts(tmp_path, max_age_s=120)]
    assert "bot_killed" in kinds


def test_a_kill_latch_file_is_pushed_even_if_the_snapshot_disagrees(tmp_path):
    p = _snap(tmp_path, "S003-live", killed=False)
    (p.parent / (p.name + ".killed")).write_text("drawdown")
    kinds = [a.kind for a in build_alerts(tmp_path, max_age_s=120)]
    assert "bot_killed" in kinds


# ----------------------------------------------------------- O2-04, O3-07 -- #
def test_a_deploy_does_not_ping_the_dead_man_fail(tmp_path, monkeypatch):
    _snap(tmp_path, age_s=600)
    (tmp_path / MAINTENANCE_NAME).write_text(str(time.time()))
    _settings(monkeypatch, tmp_path)
    posts = _record_posts(monkeypatch)
    assert mod.main([]) == 0
    pings = [url for url, _ in posts if "hc-ping" in url]
    assert pings == ["https://hc-ping.com/u"], f"a routine deploy pinged {pings}"


def test_a_demo_deploy_does_not_excuse_a_stale_live_bot(tmp_path):
    _snap(tmp_path, "S003-live", age_s=600)
    _snap(tmp_path, "S003-demo", age_s=600)
    (tmp_path / MAINTENANCE_NAME).write_text(f"{int(time.time())} demo\n")
    stale = {
        a.extra.get("bot")
        for a in build_alerts(tmp_path, max_age_s=120, expect=["S003-live", "S003-demo"])
        if a.kind == "snapshot_stale"
    }
    assert stale == {"S003-live"}


def test_a_message_run_does_not_ping_the_dead_man(tmp_path, monkeypatch):
    _snap(tmp_path)
    _settings(monkeypatch, tmp_path)
    posts = _record_posts(monkeypatch)
    mod.main(["--message", "please install", "--kind", "deploy_action_needed", "--severity",
              "warning"])  # fmt: skip
    assert not [url for url, _ in posts if "hc-ping" in url], (
        "a notice run pinged the dead-man: a hung timer would read healthy"
    )


# ------------------------------------------------------------------ O2-02 -- #
def test_a_recreated_journal_is_read_from_the_start(tmp_path):
    j = Journal(tmp_path / "journal-demo-S003.db")
    j.event("close_refused", severity="critical", instrument="SOL", reason="MANUAL ACTION")
    got = collect(j, since_seq=500)
    assert any(a.kind == "close_refused" for a in got), "cursor above the journal hid it"


# ------------------------------------------------------------------ O3-09 -- #
def test_no_dollar_amount_leaves_the_box_in_alert_text(tmp_path):
    _snap(tmp_path, account_equity_usd=37.32, sizing_equity_usd=100.0)
    lines = [redact(a) for a in build_alerts(tmp_path, max_age_s=120)]
    assert lines and not any("$" in s or "37.32" in s for s in lines), lines

    hwm = Alert("hwm_implausible", "critical", "stored=5101.70 equity=1000.00", {})
    assert "5101" not in redact(hwm) and "1000.00" not in redact(hwm), redact(hwm)


# ------------------------------------------------------------------ O3-10 -- #
def test_the_alerter_never_deletes_a_bots_snapshot(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    (d / "S003-live.json").write_text("{half a fi")
    build_alerts(tmp_path, max_age_s=120)
    assert (d / "S003-live.json").exists(), "the alerter deleted the bot's own file"
