"""`ops.unkill` is the ONLY way back from a KILL. It must not crash.

Measured in the guard drill, 2026-09-24: after a real KILL flattened a real
position and latched, clearing the latch failed with a traceback --

    RuntimeError: demo session without NTX_EQUITY_OVERRIDE_USD would size
    positions against ~$95k of play money

-- because `main()` builds a whole node `plan()` just to find a base URL for a
balance read. `plan()` enforces trading preconditions that have nothing to do
with clearing a flag, and on the box the demo override lives in the systemd unit
rather than `.env`, so an operator running the documented recovery command by
hand hits exactly this.

That matters more than most bugs here: the snapshot health check tells whoever
is on call "KILL is latched: the bot is halted and will not enter -- run
ntx.ops.unkill". At that moment the bot is not trading, something has already
gone wrong, and the recovery tool must work.

It must also be runnable without a human at a prompt, so a drill or a runbook
step can exercise it.

Written before the fix (R41).
"""

from __future__ import annotations

import pytest

from ntx.config.settings import Settings
from ntx.ops import unkill


def _settings(tmp_path, **over) -> Settings:
    kw = {
        "NTX_VENUE": "okx_demo",
        "OKX_DEMO_API_KEY": "k",
        "OKX_DEMO_API_SECRET": "s",
        "OKX_DEMO_API_PASSPHRASE": "p",
        "NTX_DATA_DIR": str(tmp_path),
    }
    kw.update(over)
    return Settings(_env_file=None, **kw)


def _latched(tmp_path, name="S003-demo.json.killed"):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    p = state / name
    p.write_text("killed_at=2026-09-24T12:57:00Z\nwhy=drawdown 30.0% >= 20.0%\n")
    return p


def test_clearing_a_latch_does_not_need_a_tradeable_configuration(monkeypatch, tmp_path):
    """No sizing override, exactly as on the box: the demo basis lives in the
    systemd unit. Clearing a flag must not care."""
    s = _settings(tmp_path)
    latch = _latched(tmp_path)
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)
    monkeypatch.setattr(unkill, "load", lambda: s, raising=False)
    monkeypatch.setenv("NTX_UNKILL_YES", unkill.CONFIRM)
    rc = unkill.main(
        ["--clear", "--bot", "S003-demo", "--equity", "1234.56", "--note", "guard drill"]
    )
    assert rc == 0, "the documented recovery command failed"
    assert not latch.exists(), "the latch is still there, so the bot stays halted"


def test_it_can_run_without_a_human_at_a_prompt(monkeypatch, tmp_path):
    """A runbook step and a drill both need this; `input()` makes it impossible."""
    s = _settings(tmp_path)
    _latched(tmp_path)
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)
    monkeypatch.setattr(unkill, "load", lambda: s, raising=False)

    def no_human(_prompt=""):
        raise AssertionError("asked a human when NTX_UNKILL_YES was set")

    monkeypatch.setattr("builtins.input", no_human)
    monkeypatch.setenv("NTX_UNKILL_YES", unkill.CONFIRM)
    assert (
        unkill.main(["--clear", "--bot", "S003-demo", "--equity", "1234.56", "--note", "drill"])
        == 0
    )


def test_without_the_env_phrase_a_human_is_still_asked(monkeypatch, tmp_path):
    """The confirmation is a real safety gate, not decoration: resuming trading
    after a KILL is a decision."""
    s = _settings(tmp_path)
    latch = _latched(tmp_path)
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)
    monkeypatch.setattr(unkill, "load", lambda: s, raising=False)
    monkeypatch.delenv("NTX_UNKILL_YES", raising=False)
    monkeypatch.setattr("builtins.input", lambda _p="": "no")
    assert unkill.main(["--clear", "--bot", "S003-demo", "--equity", "1234.56", "--note", "x"]) != 0
    assert latch.exists(), "an unconfirmed resume must leave the bot halted"


def test_status_never_needs_credentials_or_a_plan(monkeypatch, tmp_path):
    s = _settings(tmp_path)
    _latched(tmp_path)
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)
    monkeypatch.setattr(unkill, "load", lambda: s, raising=False)
    assert unkill.main(["--status"]) == 1  # 1 = latched


def test_no_latch_is_not_an_error(monkeypatch, tmp_path):
    s = _settings(tmp_path)
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)
    monkeypatch.setattr(unkill, "load", lambda: s, raising=False)
    assert unkill.main(["--status"]) == 0


def test_the_health_check_names_the_tool_that_actually_works():
    """snapshot_check tells the operator to run this; the name must match."""
    import inspect

    from ntx.ops import snapshot_check

    assert "ntx.ops.unkill" in inspect.getsource(snapshot_check.problems)


@pytest.mark.parametrize("flag", ["--clear", "--status"])
def test_neither_mode_builds_a_node_plan(flag):
    """`plan()` enforces trading preconditions. Clearing a flag is not trading."""
    import inspect

    code = [
        ln for ln in inspect.getsource(unkill.main).splitlines() if not ln.strip().startswith("#")
    ]
    assert not [ln for ln in code if "plan(" in ln], (
        "unkill still depends on a tradeable configuration"
    )


# ---- L3-01: unkill acted on every bot, with whichever account .env named ---- #


def _two_latches(tmp_path):
    return _latched(tmp_path, "S003-demo.json.killed"), _latched(tmp_path, "S003-live.json.killed")


def _go(monkeypatch, s):
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)
    monkeypatch.setattr(unkill, "load", lambda: s, raising=False)
    monkeypatch.setenv("NTX_UNKILL_YES", unkill.CONFIRM)


def test_clearing_one_bot_leaves_the_other_bots_latch_alone(monkeypatch, tmp_path):
    """The box's .env says demo; clearing the demo latch used to clear LIVE's too,
    and live re-KILLed and flattened 20 minutes after its next restart."""
    demo, live = _two_latches(tmp_path)
    _go(monkeypatch, _settings(tmp_path))
    assert unkill.main(["--clear", "--bot", "S003-demo", "--equity", "100", "--note", "x"]) == 0
    assert not demo.exists()
    assert live.exists(), "the live latch was cleared by a demo recovery"


def test_clear_without_naming_the_bot_is_refused(monkeypatch, tmp_path):
    demo, live = _two_latches(tmp_path)
    _go(monkeypatch, _settings(tmp_path))
    assert unkill.main(["--clear", "--equity", "100", "--note", "x"]) != 0
    assert demo.exists() and live.exists()


def test_a_bot_on_another_account_needs_its_equity_given(monkeypatch, tmp_path):
    """.env holds the demo key: measuring 'equity' for live with it reads the wrong account."""
    _demo, live = _two_latches(tmp_path)
    _go(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(
        "ntx.ops.okx_rest.signed",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("read the wrong account")),
    )
    assert unkill.main(["--clear", "--bot", "S003-live", "--note", "x"]) != 0
    assert live.exists()


def test_rebase_goes_to_the_named_bots_journal(monkeypatch, tmp_path):
    from ntx.obs.journal import Journal

    _go(monkeypatch, _settings(tmp_path))
    rc = unkill.main(
        ["--after-transfer", "--bot", "S003-live", "--equity", "150", "--note", "deposit"]
    )
    assert rc == 0
    live = Journal(tmp_path / "journal.db")
    assert live.count_events("hwm_rebased", within_seconds=60) == 1
    assert not (tmp_path / "journal-demo-S003.db").exists(), "re-based the demo journal"


def test_the_restart_hint_names_the_bots_own_unit(monkeypatch, tmp_path, capsys):
    _two_latches(tmp_path)
    _go(monkeypatch, _settings(tmp_path))
    unkill.main(["--clear", "--bot", "S003-live", "--equity", "100", "--note", "x"])
    assert "ntx-s003.service" in capsys.readouterr().out
