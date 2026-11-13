"""R-007 O3-01 / O3-02 / O3-03: the dedup added for F070 can lose or mute real alerts.

O3-01  The dedup state was written BEFORE the push. A one-shot critical (a KILL, a
       refused close) whose first push failed was "already sent" on the retry: the
       cursor advanced, the dead-man switch went healthy, the phone never rang.
O3-02  The fingerprint was (kind, instrument-or-bot). A demo bot's
       `position_unprotected SOL` muted the live bot's for six hours -- and the demo
       bots trade the live universe.
O3-03  A journal event appears in exactly one pass (its seq is new once), and the
       dedup forgets any fingerprint absent from the current pass. So a journal
       critical repeated every 20 min was "new" every time: F070 again.

Written before the fix (R41).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from ntx.obs import alerts as mod
from ntx.obs.alerts import Alert, suppress_repeats
from ntx.obs.journal import Journal


def _snapshot(tmp_path, bot="S003-demo"):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    snap = {
        "schema": 1,
        "ts_ns": int((time.time() - 5) * 1e9),
        "strategy_id": bot.split("-")[0],
        "positions_unprotected": 0,
        "positions": [],
    }
    (d / f"{bot}.json").write_text(json.dumps(snap))


def _settings(monkeypatch, tmp_path):
    s = SimpleNamespace(
        data_dir=tmp_path,
        alert_ntfy_url="https://ntfy.sh/t",
        healthcheck_url="",
        alert_bots="S003-demo",
        alert_fills=True,
    )
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)


def test_a_critical_whose_push_failed_is_pushed_on_the_next_pass(tmp_path, monkeypatch):
    j = Journal(tmp_path / "journal-demo-S003.db")
    j.event("guard_kill", severity="critical", reason="drawdown")
    _snapshot(tmp_path)
    _settings(monkeypatch, tmp_path)

    monkeypatch.setattr(mod, "_post", lambda *a, **k: (_ for _ in ()).throw(OSError("429")))
    assert mod.main([]) != 0

    pushed: list = []
    monkeypatch.setattr(mod, "_post", lambda url, data, headers=None: pushed.append(data))
    assert mod.main([]) == 0
    assert any(b"drawdown" in (d if isinstance(d, bytes) else str(d).encode()) for d in pushed), (
        "the KILL never reached the phone: its first failed push marked it as sent"
    )


def test_a_failed_one_shot_message_is_sent_again(tmp_path, monkeypatch):
    _snapshot(tmp_path)
    _settings(monkeypatch, tmp_path)
    args = ["--message", "deploy rolled back", "--kind", "deploy_failed"]

    monkeypatch.setattr(mod, "_post", lambda *a, **k: (_ for _ in ()).throw(OSError("429")))
    mod.main(args)

    pushed: list = []
    monkeypatch.setattr(mod, "_post", lambda url, data, headers=None: pushed.append(data))
    mod.main(args)
    assert pushed, "a one-shot whose push failed was remembered as delivered"


def _a(kind, *, source="", **extra):
    return Alert(kind, "critical", f"{kind} {extra}", extra, 0, source)


def test_a_demo_incident_does_not_mute_the_same_incident_on_live(tmp_path):
    demo = _a("position_unprotected", source="journal-demo-S003", instrument="SOL-USDT-SWAP.OKX")
    live = _a("position_unprotected", source="journal-live-S003", instrument="SOL-USDT-SWAP.OKX")
    assert suppress_repeats([demo], tmp_path, now=0.0) == [demo]
    assert suppress_repeats([demo, live], tmp_path, now=300.0) == [live]


def test_snapshot_alerts_of_two_bots_are_two_facts(tmp_path):
    demo = _a("snapshot_missing", bot="S003-demo")
    live = _a("snapshot_missing", bot="S003-live")
    suppress_repeats([demo], tmp_path, now=0.0)
    assert suppress_repeats([demo, live], tmp_path, now=300.0) == [live]


def test_more_naked_positions_is_news(tmp_path):
    one = _a("positions_unprotected", bot="S003-live", count=1)
    two = _a("positions_unprotected", bot="S003-live", count=2)
    suppress_repeats([one], tmp_path, now=0.0)
    assert suppress_repeats([two], tmp_path, now=300.0) == [two]


def test_a_journal_critical_repeated_every_pass_is_pushed_once(tmp_path):
    """Each recurrence has a new seq, so it is in one pass and absent from the next."""

    def event(seq):
        return Alert(
            "phantom_flatten_refused", "critical", "LINK", {"instrument": "LINK"}, seq, "journal"
        )

    assert suppress_repeats([event(1)], tmp_path, now=0.0) == [event(1)]
    assert suppress_repeats([], tmp_path, now=600.0) == []
    assert suppress_repeats([event(2)], tmp_path, now=1200.0) == [], "F070 through the journal"
