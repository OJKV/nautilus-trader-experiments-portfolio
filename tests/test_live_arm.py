"""Live trading needs an explicit, separate arming step (user request 2026-09-19).

The live OKX key now sits in the server `.env` alongside the demo key, because
re-pasting it at go-live is error-prone too. That makes `NTX_VENUE=okx_live` --
one word in one file -- the only thing between a test run and real money.

So the venue selects the ACCOUNT; a second, deliberate action selects whether
real orders may be sent at all. Written before the code (R41).
"""

from __future__ import annotations

import pytest

from ntx.config.settings import Settings
from ntx.live.arm import NotArmed, arm, arm_path, disarm, is_armed, require_armed


def _settings(tmp_path, **over) -> Settings:
    kw = {
        "NTX_VENUE": "okx_live",
        "OKX_API_KEY": "k",
        "OKX_API_SECRET": "s",
        "OKX_API_PASSPHRASE": "p",
        "NTX_DATA_DIR": str(tmp_path),
    }
    kw.update(over)
    return Settings(_env_file=None, **kw)


def test_a_live_run_is_refused_until_it_is_armed(tmp_path):
    s = _settings(tmp_path)
    with pytest.raises(NotArmed, match="arm"):
        require_armed(s, observe=False, dry_run=False)


def test_arming_then_running_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("NTX_ARM_ALLOW_ANY_OWNER", "1")  # ownership is tested separately
    s = _settings(tmp_path)
    arm(s.data_dir, note="go-live 2026-09-20")
    assert is_armed(s.data_dir) is True
    require_armed(s, observe=False, dry_run=False)  # does not raise


def test_disarming_puts_the_safety_back_on(tmp_path):
    s = _settings(tmp_path)
    arm(s.data_dir, note="test")
    disarm(s.data_dir)
    assert is_armed(s.data_dir) is False
    with pytest.raises(NotArmed):
        require_armed(s, observe=False, dry_run=False)


def test_observe_and_dry_run_never_need_arming(tmp_path):
    """Both are the ways to CHECK a live key safely; neither can send an order."""
    s = _settings(tmp_path)
    require_armed(s, observe=True, dry_run=False)
    require_armed(s, observe=False, dry_run=True)


def test_demo_never_needs_arming(tmp_path):
    s = _settings(tmp_path, NTX_VENUE="okx_demo", NTX_EQUITY_OVERRIDE_USD="123.45")
    require_armed(s, observe=False, dry_run=False)


def test_the_arm_file_records_who_and_why(tmp_path):
    s = _settings(tmp_path)
    arm(s.data_dir, note="first live run")
    text = arm_path(s.data_dir).read_text()
    assert "first live run" in text


def test_arming_from_the_cli_needs_the_typed_phrase(tmp_path, monkeypatch):
    from ntx.live import arm as mod

    s = _settings(tmp_path)
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")
    assert mod.main(["--on", "--note", "x"]) != 0
    assert is_armed(s.data_dir) is False

    monkeypatch.setattr("builtins.input", lambda _prompt="": mod.CONFIRM)
    assert mod.main(["--on", "--note", "x"]) == 0
    assert is_armed(s.data_dir) is True


def test_the_node_checks_arming_before_it_connects():
    import inspect

    from ntx.live import node

    src = inspect.getsource(node.main)
    assert "require_armed" in src
    connect = src.index("TradingNode(")
    assert src.index("require_armed") < connect, "check before anything reaches the venue"


def test_the_kill_switch_never_needs_arming():
    """`flatten --live` closes positions. It must work when the bot is disarmed,
    stopped, or broken -- that is the whole point of it."""
    import inspect

    from ntx.ops import flatten

    assert "require_armed" not in inspect.getsource(flatten)


# ------------------------------------------------------------------ L2-01 --- #
def test_an_arm_file_the_bot_user_could_have_written_does_not_arm_live(tmp_path, monkeypatch):
    """A push to GitHub runs as the bot user. If that user can create the arm
    file, a compromised commit can switch on real-money trading. So for LIVE the
    file must be owned by root -- which only a human with the admin password can
    produce -- wherever it lives."""
    monkeypatch.delenv("NTX_ARM_ALLOW_ANY_OWNER", raising=False)
    s = _settings(tmp_path)
    arm(s.data_dir, note="written by the bot user")  # owned by whoever runs the test
    with pytest.raises(NotArmed, match="root"):
        require_armed(s, observe=False, dry_run=False)


def test_a_root_owned_arm_file_arms_live(tmp_path, monkeypatch):
    from ntx.live import arm as mod

    monkeypatch.delenv("NTX_ARM_ALLOW_ANY_OWNER", raising=False)
    s = _settings(tmp_path)
    arm(s.data_dir, note="go-live")
    monkeypatch.setattr(mod, "_owner_uid", lambda path: 0)
    require_armed(s, observe=False, dry_run=False)  # does not raise


def test_demo_never_needs_a_root_owned_file(tmp_path, monkeypatch):
    monkeypatch.delenv("NTX_ARM_ALLOW_ANY_OWNER", raising=False)
    s = _settings(tmp_path, NTX_VENUE="okx_demo", NTX_EQUITY_OVERRIDE_USD="123.45")
    require_armed(s, observe=False, dry_run=False)


def test_status_works_when_the_arm_file_is_readable_by_root_only(tmp_path, monkeypatch, capsys):
    """2026-10-07: after the move to /etc/ntx (0600 root), `--status` as the bot
    user printed ARMED and then crashed reading the note."""
    from types import SimpleNamespace

    import ntx.live.arm as arm

    f = tmp_path / "LIVE_ARMED"
    f.write_text("armed_by=root\nnote=x\n")
    monkeypatch.setenv("NTX_ARM_FILE", str(f))
    monkeypatch.setattr(arm, "load", lambda: SimpleNamespace(data_dir=tmp_path), raising=False)
    monkeypatch.setattr("ntx.config.settings.load", lambda: SimpleNamespace(data_dir=tmp_path))
    real = type(f).read_text

    def denied(self, *a, **k):
        if self == f:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(type(f), "read_text", denied)
    assert arm.main(["--status"]) == 0
    assert "ARMED" in capsys.readouterr().out


def test_the_suite_never_reads_the_machines_real_arm_file():
    """2026-10-07: once /etc/ntx existed on the box, arm_path() preferred it over
    every test's tmp dir; three tests read the real root-only file and the live
    deploy rolled back. The suite must be hermetic about arming."""
    import ntx.live.arm as arm

    assert not str(arm.SYSTEM_ARM_FILE).startswith("/etc/"), arm.SYSTEM_ARM_FILE
