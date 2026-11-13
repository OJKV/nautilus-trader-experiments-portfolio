"""Review round 3 (R-007), Part 2 + Part 6: config, unit pins, per-strategy keys.

Each test here was written before its fix and observed to fail (R41).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ntx.config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"

LIVE = {
    "NTX_VENUE": "okx_live",
    "OKX_API_KEY": "live-k",
    "OKX_API_SECRET": "live-s",
    "OKX_API_PASSPHRASE": "live-p",
    "OKX_DEMO_API_KEY": "demo-k",
    "OKX_DEMO_API_SECRET": "demo-s",
    "OKX_DEMO_API_PASSPHRASE": "demo-p",
}


def _s(**over) -> Settings:
    kw = dict(LIVE)
    kw.update(over)
    return Settings(_env_file=None, **kw)


def _pins(unit: str) -> dict[str, str]:
    text = (DEPLOY / unit).read_text()
    return dict(re.findall(r"^Environment=(NTX_[A-Z_]+)=(.*)$", text, flags=re.M))


# --- E3-02 / L3-05 / L3-11: a .env line must not move a flag between units ----------


@pytest.mark.parametrize(
    "unit, keyset, gmo",
    [
        # live: rehearsal NOT promoted (docs/27). Keyset deliberately unpinned --
        # see the unit's comment; creds() refuses every other value for S003.
        ("ntx-s003.service", None, "0"),
        ("ntx-soak.service", "demo1", "1"),  # demo: owner-approved rehearsal
        ("ntx-s004-demo2.service", "demo2", "1"),
    ],
)
def test_every_node_unit_pins_its_keyset_and_the_rehearsal_flag(unit, keyset, gmo):
    """A unit that leaves a flag unpinned inherits whatever `.env` says, so one
    line there promotes a rehearsal to real money or moves the soak onto
    S004's sub-account (R51/F064). A blank pin cannot help: env_ignore_empty
    treats it as unset, so the value must be explicit."""
    pins = _pins(unit)
    assert pins.get("NTX_KEYSET") == keyset, f"{unit} must pin NTX_KEYSET={keyset}"
    assert pins.get("NTX_GENERATE_MISSING_ORDERS") == gmo, f"{unit} must pin it to {gmo}"


def test_s003_refuses_every_keyset_but_its_own():
    """Why the live unit need not pin NTX_KEYSET: a leaked value is a refusal."""
    for ks in ("demo1", "demo2", "S004", "nonsense"):
        with pytest.raises(ValueError):
            _s(NTX_KEYSET=ks).creds("S003")


def test_the_process_environment_beats_the_dotenv_file(tmp_path, monkeypatch):
    """The pin only works if a unit's Environment= wins over `.env`."""
    env = tmp_path / ".env"
    env.write_text("NTX_GENERATE_MISSING_ORDERS=1\nNTX_KEYSET=demo2\nNTX_VENUE=okx_demo\n")
    monkeypatch.setenv("NTX_GENERATE_MISSING_ORDERS", "0")
    monkeypatch.setenv("NTX_KEYSET", "demo1")
    s = Settings(_env_file=env)
    assert s.generate_missing_orders is False
    assert s.keyset == "demo1"


def test_demo1_names_the_first_demo_account():
    s = _s(NTX_VENUE="okx_demo", NTX_KEYSET="demo1")
    assert s.creds() == ("demo-k", "demo-s", "demo-p")


# --- E3-03: per-strategy LIVE keys (ADR-0014 amendment) -------------------------------


def test_a_second_live_strategy_cannot_start_on_s003s_key():
    """Today the only live key is S003's. A second live strategy on it is two
    nodes on one account -- each adopts and closes the other's positions."""
    from ntx.live.node import plan

    with pytest.raises(ValueError, match="S003's live key"):
        plan(_s(), strategy_id="S005")


def test_s003_keeps_the_existing_live_key_with_or_without_the_pin():
    from ntx.live.node import plan

    assert plan(_s(), strategy_id="S003").strategy_id == "S003"
    assert _s(NTX_KEYSET="S003").creds("S003") == ("live-k", "live-s", "live-p")
    assert _s().creds("S003") == ("live-k", "live-s", "live-p")


def test_a_live_strategy_reads_its_own_key_set(monkeypatch):
    from ntx.live.node import plan

    monkeypatch.setenv("OKX_S005_API_KEY", "s5-k")
    monkeypatch.setenv("OKX_S005_API_SECRET", "s5-s")
    monkeypatch.setenv("OKX_S005_API_PASSPHRASE", "s5-p")
    s = _s(NTX_KEYSET="S005")
    assert s.creds("S005") == ("s5-k", "s5-s", "s5-p")
    assert plan(s, strategy_id="S005").strategy_id == "S005"


def test_a_live_keyset_belongs_to_one_strategy(monkeypatch):
    monkeypatch.setenv("OKX_S005_API_KEY", "s5-k")
    monkeypatch.setenv("OKX_S005_API_SECRET", "s5-s")
    monkeypatch.setenv("OKX_S005_API_PASSPHRASE", "s5-p")
    with pytest.raises(ValueError, match="S005"):
        _s(NTX_KEYSET="S005").creds("S003")
    with pytest.raises(ValueError, match="S003"):
        _s(NTX_KEYSET="S003").creds("S005")


def test_a_per_strategy_key_that_is_s003s_key_is_refused(monkeypatch):
    """Copy-pasting S003's key under a new name is the same account again."""
    monkeypatch.setenv("OKX_S005_API_KEY", "live-k")
    monkeypatch.setenv("OKX_S005_API_SECRET", "s5-s")
    monkeypatch.setenv("OKX_S005_API_PASSPHRASE", "s5-p")
    with pytest.raises(ValueError, match="same"):
        _s(NTX_KEYSET="S005").creds("S005")


def test_a_per_strategy_keyset_without_keys_is_refused(monkeypatch):
    for part in ("KEY", "SECRET", "PASSPHRASE"):
        monkeypatch.delenv(f"OKX_S005_API_{part}", raising=False)
    with pytest.raises(ValueError, match="OKX_S005_API_"):
        _s(NTX_KEYSET="S005").creds("S005")


def test_a_demo_keyset_never_reaches_live():
    for ks in ("demo1", "demo2"):
        with pytest.raises(ValueError, match="demo"):
            _s(NTX_KEYSET=ks).creds("S003")


# --- E3-04: loss limits are range-checked -------------------------------------------


@pytest.mark.parametrize(
    "name, value",
    [
        ("NTX_DAILY_LOSS_LIMIT_EQUITY_PCT", "0"),
        ("NTX_DAILY_LOSS_LIMIT_EQUITY_PCT", "-5"),
        ("NTX_DAILY_LOSS_LIMIT_EQUITY_PCT", "50"),
        ("NTX_MAX_DRAWDOWN_EQUITY_PCT", "0"),
        ("NTX_MAX_DRAWDOWN_EQUITY_PCT", "-1"),
        ("NTX_MAX_DRAWDOWN_EQUITY_PCT", "100"),
    ],
)
def test_a_loss_limit_that_disables_its_guard_is_refused(name, value):
    """0 or a negative value disables PAUSE/KILL; 100 makes KILL unreachable."""
    with pytest.raises(ValueError, match=name):
        _s(**{name: value})


def test_the_default_loss_limits_still_load():
    s = _s()
    assert (s.daily_loss_limit_equity_pct, s.max_drawdown_equity_pct) == (5.0, 20.0)


def test_live_refuses_loss_limits_that_disagree_with_a_pinned_gate(monkeypatch):
    """R69 for the loss limits: once the registry pins them, a .env typo on live
    is a refusal, not a silent change of the kill switch."""
    from ntx.live import node
    from ntx.strategies import registry

    monkeypatch.setitem(
        registry.DEPLOYED, "S003", {**registry.DEPLOYED["S003"], "max_drawdown_equity_pct": 20.0}
    )
    p = node.plan(_s(), strategy_id="S003")
    with pytest.raises(RuntimeError, match="max_drawdown_equity_pct"):
        node.assert_matches_gate(p, _s(NTX_MAX_DRAWDOWN_EQUITY_PCT="40"))


# --- E3-05: a configuration refusal stays down (exit 2) -----------------------------


def test_a_config_refusal_exits_2_not_1(monkeypatch):
    """Exit 1 is restarted every 120 s for ~20 min before anyone is paged."""
    from ntx.live import node

    monkeypatch.setattr("ntx.config.settings.load", lambda: _s(NTX_KEYSET="demo2"))
    assert node.main(["--strategy", "S003", "--dry-run"]) == node.EXIT_REFUSED


def test_an_invalid_settings_file_exits_2(monkeypatch):
    from ntx.live import node

    monkeypatch.setenv("NTX_VENUE", "okx-paper")
    assert node.main(["--strategy", "S003", "--dry-run"]) == node.EXIT_REFUSED


# --- L3-02: the arm file cannot be faked or redirected with a symlink ---------------


class _Live:
    is_demo = False

    def __init__(self, data_dir):
        self.data_dir = data_dir


def test_a_symlink_to_a_root_owned_file_does_not_arm_live(tmp_path, monkeypatch):
    """`ln -s /etc/hosts data/LIVE_ARMED` as the bot user passed the uid-0 check:
    stat() follows the link and reports the TARGET's owner."""
    from ntx.live.arm import NotArmed, require_armed

    link = tmp_path / "LIVE_ARMED"
    link.symlink_to("/etc/hosts")  # root-owned on macOS and Linux
    monkeypatch.setenv("NTX_ARM_FILE", str(link))
    monkeypatch.delenv("NTX_ARM_ALLOW_ANY_OWNER", raising=False)
    with pytest.raises(NotArmed, match="symlink"):
        require_armed(_Live(tmp_path))


def test_arming_never_writes_through_a_planted_symlink(tmp_path, monkeypatch):
    """Root runs `arm --on`; a link planted by the bot user must not turn that
    into a root write to a file of the bot user's choosing."""
    from ntx.live.arm import arm

    target = tmp_path / "victim"
    target.write_text("original\n")
    link = tmp_path / "LIVE_ARMED"
    link.symlink_to(target)
    monkeypatch.setenv("NTX_ARM_FILE", str(link))
    path = arm(tmp_path, note="x")
    assert target.read_text() == "original\n"
    assert not path.is_symlink()


# --- L3-03: install_units.sh shows a diff and refuses a unit that would run as root -


GOOD = """[Service]
User=ntx
ExecStart=/home/ntx/ntx/.venv/bin/python -m ntx.ui --port 8788
"""


def _install(tmp_path, units: dict[str, str], installed: dict[str, str] | None = None):
    import subprocess

    repo, dest = tmp_path / "repo", tmp_path / "dest"
    (repo / "deploy").mkdir(parents=True)
    dest.mkdir()
    for name, text in units.items():
        (repo / "deploy" / name).write_text(text)
    for name, text in (installed or {}).items():
        (dest / name).write_text(text)
    env = {
        "PATH": "/usr/bin:/bin",
        "NTX_REPO": str(repo),
        "NTX_LIVE_REPO": str(tmp_path / "nolive"),
        "NTX_UNIT_DEST": str(dest),
    }
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "install_units.sh"), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "name, text, why",
    [
        ("ntx-ui.service", GOOD.replace("User=ntx", "User=root"), "User="),
        ("ntx-ui.service", GOOD.replace("User=ntx\n", ""), "User="),
        ("ntx-ui.service", GOOD.replace("/home/ntx/ntx/.venv", "/usr/bin"), "Exec"),
        ("ntx-ui.service", GOOD.replace("ExecStart=/", "ExecStart=+/"), "Exec"),
        ("ntx-ui.service", GOOD + "ExecStartPre=!/home/ntx/ntx/x\n", "Exec"),
        ("ntx-ui.service", GOOD + "AmbientCapabilities=CAP_SYS_ADMIN\n", "AmbientCapabilities"),
        ("other.service", GOOD, "ntx-"),
    ],
)
def test_a_unit_that_could_run_as_root_is_refused(tmp_path, name, text, why):
    """The checkout is ntx-writable and fast-forwarded from GitHub; the admin is
    told to install from it with sudo. A pushed `User=root` must not become root."""
    r = _install(tmp_path, {name: text})
    assert r.returncode != 0, r.stdout
    assert why in r.stdout + r.stderr
    assert "REFUS" in r.stdout + r.stderr


def test_a_changed_unit_is_shown_as_a_diff_before_install(tmp_path):
    new = GOOD + "Environment=NTX_KEYSET=demo1\n"
    r = _install(tmp_path, {"ntx-ui.service": new}, {"ntx-ui.service": GOOD})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "+Environment=NTX_KEYSET=demo1" in r.stdout


# --- L3-08 / L2-07: account tools name their account; nothing follows .env -------


@pytest.mark.parametrize(
    "module, argv",
    [
        ("ntx.ops.leverage", ["--check"]),
        ("ntx.ops.leverage", ["--set"]),
        ("ntx.ops.venue_state", []),
        ("ntx.ops.restart_cost", []),
    ],
)
def test_a_money_state_tool_without_live_or_demo_is_refused(module, argv):
    """Whichever NTX_VENUE the box's .env holds, half the documented commands
    pointed at the other account. The account is now always named."""
    import importlib

    with pytest.raises(SystemExit) as e:
        importlib.import_module(module).main(argv)
    assert e.value.code == 2


def test_set_live_proves_the_live_account_flat_even_when_env_says_demo(monkeypatch):
    """`leverage --set --live` checked flatness with a flagless venue_state, i.e.
    on the DEMO account when .env said demo (L2-07)."""
    from ntx.ops import leverage as lv

    seen = {}
    monkeypatch.setattr(lv, "load", lambda: _s(NTX_VENUE="okx_demo"))
    monkeypatch.setattr(lv, "read_leverage", lambda ids, s, demo, base=None: {})

    def fake_state(argv):
        seen["argv"] = argv
        return 1  # "not flat": stop before anything is set

    monkeypatch.setattr("ntx.ops.venue_state.main", fake_state)
    assert lv.main(["--set", "--live", "--only", "SOL"]) == 2
    assert seen["argv"] == ["--live"]


def test_the_suite_never_inherits_a_commit_hooks_git_environment():
    """A pre-commit hook runs this suite with GIT_DIR / GIT_INDEX_FILE set. Tests
    that `git init` a scratch checkout then wrote into the REAL repository:
    `core.bare=true` and `user.name=t` landed in the shared .git/config,
    breaking every worktree and the owner's checkout (seen 2026-10-07)."""
    import os

    leaked = sorted(k for k in os.environ if k in _HOOK_GIT_VARS)
    assert not leaked, f"conftest must scrub {leaked}"


_HOOK_GIT_VARS = {
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
}


def test_the_real_units_pass_the_installer_review(tmp_path):
    units = {p.name: p.read_text() for p in DEPLOY.glob("ntx-*.service")}
    units.update({p.name: p.read_text() for p in DEPLOY.glob("ntx-*.timer")})
    r = _install(tmp_path, units)
    assert r.returncode == 0, r.stdout + r.stderr
