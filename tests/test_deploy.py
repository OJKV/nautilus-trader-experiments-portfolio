"""The deploy path is code too, and it is the one piece no other test imports.

A syntax error in `update.sh` breaks the machine that would otherwise fix it
(ARGUS F-083), so these tests pin the shape of the scripts and the units rather
than their prose.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = [
    ROOT / "deploy" / "update.sh",
    ROOT / "scripts" / "journal_backup.sh",
    ROOT / "scripts" / "vps_check.sh",
    ROOT / "scripts" / "setup_live_checkout.sh",
]
UNITS = sorted(ROOT.glob("deploy/*.service")) + sorted(ROOT.glob("deploy/*.timer"))
UPDATE = (ROOT / "deploy" / "update.sh").read_text()
SETUP = (ROOT / "scripts" / "setup_live_checkout.sh").read_text()
# The two checkouts (docs/24 §1b). Every script and the live unit must
# agree on these, which is what test_the_two_checkouts_never_collide checks.
DEMO_REPO = "/home/ntx/ntx"
LIVE_REPO = "/home/ntx/ntx-live"


def _code(text: str) -> str:
    """The shell source with whole-line comments removed.

    Every one of these scripts explains itself at length, and half the explanation
    is about what it must NOT do -- so a naive substring search finds the live
    unit's name in a comment saying it is never touched.
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))


def _shell_code(text: str) -> str:
    """_code(), and also without heredoc bodies.

    The usage text these scripts print is full of the commands a HUMAN should
    run -- `sudo systemctl stop ntx-s003.service` among them. Searching the raw
    file for what the script itself may not do finds the instructions instead.

    `log "..."` lines are the same thing by another route: when a deploy refuses,
    it has to tell the operator the privileged command that would fix it. Those
    are messages, not privileges, so they are stripped too.
    """
    out, skip_until = [], None
    for line in _code(text).splitlines():
        if skip_until is not None:
            if line.strip() == skip_until:
                skip_until = None
            continue
        m = re.search(r"<<-?\"?'?([A-Z][A-Z_]*)'?\"?", line)
        if m:
            skip_until = m.group(1)
            continue
        if re.match(r"\s*log\s", line):  # a message to a human, not an action
            continue
        out.append(line)
    return "\n".join(out)


@pytest.mark.parametrize("script", SHELL, ids=lambda p: p.name)
def test_every_shell_script_parses(script):
    assert script.exists(), script
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_update_is_executable():
    assert (ROOT / "deploy" / "update.sh").stat().st_mode & 0o111


def test_the_rollback_trap_is_armed_before_the_merge():
    """ARGUS F-078: a failure during the merge or the sync must still be undone."""
    trap = UPDATE.index("trap rollback ERR")
    merge = UPDATE.index("git merge --ff-only")
    assert trap < merge


def test_the_deploy_installs_the_same_environment_as_make_install():
    """ARGUS F-082: a mismatched sync deletes the tooling the deploy gate needs."""
    makefile = (ROOT / "Makefile").read_text()
    install = next(ln for ln in makefile.splitlines() if ln.strip().startswith("uv sync"))
    flags = set(install.split()[2:])
    sync = next(ln for ln in UPDATE.splitlines() if ln.startswith("sync_env()"))
    assert flags <= set(sync.split()), f"{flags} vs {sync}"


def test_a_changed_update_script_re_execs_itself_after_a_syntax_check():
    """ARGUS F-083, and the syntax check must come before the exec."""
    check = UPDATE.index("bash -n deploy/update.sh")
    ex = UPDATE.index('exec "$REPO/deploy/update.sh"')
    assert check < ex


def test_an_already_current_checkout_still_verifies_the_environment():
    """ARGUS F-081: a half-finished sync leaves new code on old dependencies."""
    block = UPDATE[UPDATE.index('if [ "$OLD" = "$NEW" ]') : UPDATE.index('log "updating')]
    assert "sync_env" in block and "pytest" in block


def test_two_deploys_cannot_run_at_once():
    assert "flock -n 9" in UPDATE


def test_the_tests_run_on_the_box_before_anything_restarts():
    run_tests = UPDATE.index('log "tests pass"')
    restart = UPDATE.index('for u in $STOPPED; do\n  log "starting $u"')
    assert run_tests < restart


def test_the_live_unit_is_never_touched_automatically():
    """R14, now structural rather than conditional: the deployer runs in the DEMO
    checkout, and the only unit it can name is the soak. It used to list
    ntx-s003.service in units_active() and rely on a refusal further down to stop
    itself before `systemctl stop` reached the live node."""
    units = _code(UPDATE[UPDATE.index("units_active() {") : UPDATE.index("snapshot_args() {")])
    assert "$MANAGED_UNITS" in units, "the deployer must not name units inline"
    assert "ntx-s003" not in units, "a demo deploy must not be able to name the live unit"
    # The list grows as demo bots are added (F072); what must never change is
    # that the money node is not in it.
    managed = next(ln for ln in UPDATE.splitlines() if "MANAGED_UNITS=" in ln and "soak" in ln)
    assert "ntx-s003.service" not in managed
    assert 'MANAGED_UNITS=""' in UPDATE, "the live role manages no units at all"


def test_the_node_is_stopped_before_the_code_changes_under_it():
    """`ntx` is installed editable: a running node imports the new code at its
    next lazy import (write_state -> ntx.obs.atomic, every 15 s)."""
    stop = UPDATE.index('sudo -n systemctl stop "$u"')
    merge = UPDATE.index("git merge --ff-only")
    assert stop < merge


def test_the_journal_is_backed_up_before_every_deploy():
    backup = UPDATE.index("journal_backup.sh")
    # the merge that can change code; the docs-only fast path merges earlier and
    # touches nothing a bot runs, so there is nothing to back up against
    merge = UPDATE.rindex("git merge --ff-only")
    assert backup < merge


def test_unit_file_drift_is_reported():
    """A git pull cannot update /etc/systemd/system; silence there means running
    an old unit against new code."""
    assert "check_unit_drift" in UPDATE and "/etc/systemd/system/" in UPDATE


def test_a_restart_is_verified_by_a_healthy_state_snapshot():
    """R31: verify the read-back. 'systemctl start returned 0' is not proof."""
    assert "wait_healthy" in UPDATE and "ntx.ops.snapshot_check" in UPDATE


def test_the_deploy_never_touches_the_journal_or_the_data_directory():
    """R29. The only write under data/ is the lock file."""
    for bad in ("rm -rf", "rm -f data", "journal.db", "git clean"):
        assert bad not in UPDATE, bad


# ------------------------------------------------------------------- units -- #
@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_units_have_no_relative_paths(unit):
    """R21: systemd's working directory is not the repo."""
    for line in unit.read_text().splitlines():
        if line.startswith(("ExecStart", "ExecStartPost", "WorkingDirectory", "EnvironmentFile")):
            path = line.split("=", 1)[1].split()[0]
            assert path.startswith("/"), f"{unit.name}: {line}"


@pytest.mark.parametrize(
    "unit", [u for u in UNITS if u.name.endswith(".service")], ids=lambda p: p.name
)
def test_services_run_as_the_bot_user(unit):
    text = unit.read_text()
    if "ExecStart" in text:
        assert "User=ntx" in text, unit.name


def test_the_live_unit_pins_the_venue_and_the_experiment_flags():
    text = (ROOT / "deploy" / "ntx-s003.service").read_text()
    assert "Environment=NTX_VENUE=okx_live" in text
    assert "Environment=NTX_ATTACH_STOP_AT_ENTRY=false" in text


def test_a_deliberate_refusal_is_not_restarted_in_a_loop():
    """Exit 2 = lock held or not armed. Restarting cannot fix either."""
    for name in ("ntx-s003.service", "ntx-soak.service"):
        text = (ROOT / "deploy" / name).read_text()
        assert "RestartPreventExitStatus=2" in text, name
        assert "Restart=always" in text, name


@pytest.mark.parametrize("name", ["sudoers-ntx-deploy", "sudoers-ntx-ops"])
def test_the_sudoers_drop_ins_grant_no_wildcards(name):
    text = (ROOT / "deploy" / name).read_text()
    rules = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert rules
    for rule in rules:
        assert "*" not in rule, rule
        assert "ALL=(ALL)" not in rule, rule
        assert "NOPASSWD: ALL" not in rule, rule


def test_the_ops_rule_cannot_install_unit_files_or_touch_another_project():
    """The two things that would turn "operate the bot" into "own the box"."""
    text = (ROOT / "deploy" / "sudoers-ntx-ops").read_text()
    body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
    for forbidden in ("install", "/bin/sh", "/bin/bash", "argus", "visudo", "usermod", "cp "):
        assert forbidden not in body, forbidden
    # `enable`/`disable` decide what comes back after a reboot -- operator only.
    assert "systemctl enable" not in body and "systemctl disable" not in body


def test_the_update_timer_runs_the_update_service():
    timer = (ROOT / "deploy" / "ntx-update.timer").read_text()
    assert "Unit=ntx-update.service" in timer
    assert (ROOT / "deploy" / "ntx-update.service").exists()


# ---------------------------------------------------------------------- CI -- #
def test_the_github_gate_runs_the_same_checks_as_the_box():
    gate = (ROOT / ".github" / "workflows" / "gate.yml").read_text()
    assert "uv run pytest -q" in gate
    assert "ruff check" in gate
    assert "bash -n deploy/update.sh" in gate, "a broken deploy script must fail CI"


def test_no_secret_or_server_address_in_the_tracked_deploy_assets():
    for path in [*SHELL, *UNITS, ROOT / ".github/workflows/gate.yml"]:
        text = path.read_text()
        ips = [
            ip
            for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
            if not ip.startswith(("127.", "0.0.0.0", "10.", "192.168."))
        ]
        assert not ips, f"{path.name}: {ips}"
        assert "OKX_API" not in text or "OKX_API_KEY=" not in text, path.name


def test_the_rollback_target_survives_the_re_exec():
    """After `exec`, HEAD is already the new commit: taking the rollback target
    from git there would "roll back" to the commit being undone."""
    branch = UPDATE[UPDATE.index('if [ "${NTX_DEPLOY_REEXEC:-}" = "1" ]') :]
    branch = branch[: branch.index("else")]
    assert "OLD=${NTX_DEPLOY_OLD:-$OLD}" in branch
    assert branch.index("OLD=${NTX_DEPLOY_OLD:-$OLD}") < branch.index("trap rollback ERR")


def test_a_tick_with_no_new_commit_does_not_re_run_the_whole_suite():
    """F051: with the timer at 1 minute, the 'verify the environment' branch ran
    `uv sync` + the full pytest suite on EVERY tick -- a permanent test treadmill
    on the box that is supposed to be trading. Re-verification is stamped and
    rate-limited; an ordinary tick is a fetch and an exit."""
    block = UPDATE[UPDATE.index('if [ "$OLD" = "$NEW" ]') : UPDATE.index('log "updating')]
    assert "stamp_is_fresh" in block, "no stamp check: every tick re-verifies"
    assert "STAMP=" in UPDATE and "VERIFY_EVERY" in UPDATE, "no rate limit on re-verification"
    # and the stamp must be written after a successful deploy too
    assert UPDATE.count("write_stamp") >= 2


# ------------------------------------------------------------- live deploy -- #
LIVE = (ROOT / "deploy" / "deploy-live.sh").read_text()


def test_the_live_deploy_checks_the_venue_before_anything_else():
    """A restart re-places the venue stop (F044). Doing that over an open
    position is the one thing this script exists to prevent."""
    check = LIVE.index("ntx.ops.venue_state --live")
    update = LIVE.index('"$LIVE_REPO/deploy/update.sh"')  # the call, not the comment
    assert check < update


def test_the_live_deploy_needs_a_typed_confirmation():
    assert 'CONFIRM="DEPLOY LIVE"' in LIVE
    # $NEED is whichever sentence this run demands: the ordinary one when the
    # account is flat, the harder one when it is not (2026-09-27).
    confirm = LIVE.index("Type $NEED to continue")
    update = LIVE.index('"$LIVE_REPO/deploy/update.sh"')
    assert confirm < update, "confirmation must come before anything is touched"


def test_the_live_deploy_verifies_the_node_came_back():
    assert "snapshot_check" in LIVE and "--live" in LIVE
    assert LIVE.index("snapshot_check") > LIVE.index('"$LIVE_REPO/deploy/update.sh"')


def test_the_live_deploy_is_never_wired_to_a_timer():
    """Only the demo soak is deployed automatically (docs/24 section 4)."""
    for unit in [*ROOT.glob("deploy/*.timer"), *ROOT.glob("deploy/*.service")]:
        assert "deploy-live" not in _code(unit.read_text()), unit.name


# ------------------------------------------------------------------- L-02 --- #
def test_a_node_that_never_starts_trading_kills_itself():
    """nautilus logs and re-raises an exception from `on_start`, leaving the
    strategy in STARTING while the process stays up: systemd sees `active`, the
    snapshot is never written, and nothing trades. A failed adoption (L-09) lands
    here too. So the node checks itself after a grace period and exits loudly."""
    from types import SimpleNamespace

    from ntx.live.node import trader_is_running, watch_startup

    running = SimpleNamespace(trader=SimpleNamespace(is_running=True))
    stuck = SimpleNamespace(trader=SimpleNamespace(is_running=False))
    assert trader_is_running(running) is True
    assert trader_is_running(stuck) is False

    killed = []
    watch_startup(stuck, grace_s=0, sleep=lambda s: None, exit_fn=killed.append)
    assert killed == [3], "a node that is up but not trading must not look healthy"

    killed.clear()
    watch_startup(running, grace_s=0, sleep=lambda s: None, exit_fn=killed.append)
    assert killed == []


def test_the_deploy_marks_maintenance_so_it_does_not_page_anyone():
    """F053: stopping the node to swap code outlasts the staleness limit, so
    every deploy fired a critical and pinged the dead-man switch."""
    from ntx.obs.alerts import MAINTENANCE_NAME

    assert MAINTENANCE_NAME in UPDATE
    mark = UPDATE.index(MAINTENANCE_NAME)
    stop = UPDATE.index('sudo -n systemctl stop "$u"')
    assert mark < stop, "the marker must exist before the node goes down"
    assert "trap" in UPDATE and "clear_maintenance" in UPDATE, "it must be cleared on any exit"


def test_nothing_kills_the_monitor_by_matching_its_command_line():
    """`pkill -f "ntx.ui --port 8788"` also matches the ssh wrapper shell that is
    running it, so it kills its own session (exit 255, 2026-09-20).

    `make ui-vps` no longer kills anything -- it is a tunnel now, and the monitor
    is ntx-ui.service. The hazard survives the redesign, though: any recipe that
    reaches over ssh to stop the monitor must find it by WHO IS LISTENING ON THE
    PORT, never by matching the command line.
    """
    mk = (ROOT / "Makefile").read_text()
    commands = "\n".join(ln for ln in mk.splitlines() if not ln.strip().lstrip("@").startswith("#"))
    assert 'pkill -f "ntx.ui' not in commands, (
        "matching the command line over ssh kills the ssh session itself"
    )


# --------------------------------------------- round-2 deployer findings ---- #
def test_rollback_stops_before_it_resets():
    """L2-06: on the unhealthy-start path the unit is already running, so the
    rollback's `systemctl start` was a no-op and the node kept running the bad
    code from memory with the old code on disk."""
    body = UPDATE[UPDATE.index("rollback() {") : UPDATE.index('if [ "${NTX_DEPLOY_REEXEC:-}"')]
    assert "systemctl stop" in body, "rollback must stop what it started"
    assert body.index("systemctl stop") < body.index("git reset --hard")


def test_rollback_says_so_when_a_unit_stays_down():
    body = UPDATE[UPDATE.index("rollback() {") : UPDATE.index('if [ "${NTX_DEPLOY_REEXEC:-}"')]
    assert "ROLLBACK INCOMPLETE" in body


def test_a_live_node_in_auto_restart_is_still_a_live_node():
    """L2-02: `systemctl is-active` is false while a unit is in
    `activating (auto-restart)` -- its state for the whole RestartSec window
    after a crash. Advancing the live checkout in that window swaps the code the
    node imports at its next restart, so the check is on ActiveState."""
    # the ActiveState check is shared with the demo units since L3-09
    fn = UPDATE[UPDATE.index("unit_down() {") : UPDATE.index("check_unit_drift() {")]
    assert "unit_down ntx-s003.service" in fn
    assert "ActiveState" in fn, "is-active cannot see auto-restart"
    assert "inactive|failed" in fn
    # and it gates the merge, in both roles-worth of sampling (before and after)
    assert UPDATE.count("live_unit_down") >= 3


def test_a_hand_rollback_survives_the_next_timer_tick():
    """D2-01. The arm file and `is-enabled` used to stand in for this; now the
    live checkout is never advanced automatically at all, and the hold file is
    what pins the DEMO checkout after a hand rollback."""
    hold = UPDATE.index(".deploy-hold")
    merge = UPDATE.index("git merge --ff-only")
    assert hold < merge


def test_a_deploy_hold_stops_the_timer_from_undoing_a_manual_rollback():
    assert "deploy-hold" in UPDATE
    hold = UPDATE.index("deploy-hold")
    merge = UPDATE.index("git merge --ff-only")
    assert hold < merge


def test_the_bot_user_cannot_start_or_stop_the_live_unit():
    """L2-01: the account that a pushed commit runs as must not be able to reach
    the live switches. Restarting the live node belongs to the admin account."""
    text = (ROOT / "deploy" / "sudoers-ntx-deploy").read_text()
    rules = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    ntx_rules = [r for r in rules if r.split()[0] == "ntx"]
    assert ntx_rules, "the bot user still needs to restart its own soak"
    assert not any("ntx-s003" in r for r in ntx_rules), ntx_rules


def test_the_arm_file_lives_outside_the_checkout():
    """A commit that runs as the bot user must not be able to arm live trading."""
    import inspect

    from ntx.live import arm as mod

    src = inspect.getsource(mod)
    assert "NTX_ARM_FILE" in src, "the path must be overridable"
    assert "/etc/ntx" in src, "the default belongs outside the repo checkout"


# ------------------------------------------------------ vps_check SIGPIPE --- #
VPS_CHECK = (ROOT / "scripts" / "vps_check.sh").read_text()


def _fake_systemctl(tmp_path: Path) -> Path:
    """A `systemctl` whose unit list is far larger than a pipe buffer.

    That is what the real box looks like (~26 KB, ~600 units) and it is the
    condition under which a `... | grep -q` pipeline under `set -o pipefail`
    returns 141: grep exits on the first match, the writer gets EPIPE, and the
    *successful* match is reported as a failure.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "pat=''\n"
        'for a in "$@"; do case "$a" in list-unit-files|--no-legend|--no-pager|--) ;;\n'
        '  *) pat="$a";; esac; done\n'
        "{ echo 'ntx-soak.service enabled enabled'\n"
        '  for i in $(seq 1 4000); do echo "filler-$i.service enabled enabled"; done; }'
        ' | { if [ -n "$pat" ]; then grep -F -- "$pat"; else cat; fi; }\n'
    )
    fake.chmod(0o755)
    return bindir


def test_a_running_unit_is_never_reported_as_not_installed(tmp_path):
    """The readiness check must not tell an operator that a running unit is absent.

    It did: `have_unit` was `systemctl list-unit-files | grep -q "^$1"` in a
    script with `set -o pipefail`, and on the real box it answered "installed"
    for only 7-8 of 15 identical calls. Every wrong answer is the reassuring
    one -- "not installed yet (expected until go-live)" -- and it also silences
    the warning that the LIVE money unit is running.
    """
    body = next(ln for ln in VPS_CHECK.splitlines() if ln.startswith("have_unit()"))
    bindir = _fake_systemctl(tmp_path)
    script = f"set -uo pipefail\n{body}\nhave_unit ntx-soak.service\n"
    found = sum(
        subprocess.run(
            ["bash", "-c", script],
            env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)},
            capture_output=True,
        ).returncode
        == 0
        for _ in range(15)
    )
    assert found == 15, f"have_unit found an installed unit only {found}/15 times"


def test_no_pipefail_script_lets_an_early_exit_reader_fail_a_pipeline():
    """`cmd | head -N` and `cmd | grep -q` under `pipefail` report 141 whenever
    the reader finishes first. Anywhere the status is used, guard it."""
    offenders = []
    for path in SHELL:
        text = path.read_text()
        if "pipefail" not in text:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "|" not in line:
                continue
            if re.search(r"\|\s*(grep\s+-[a-zA-Z]*q|head\s+-)", line) and "|| true" not in line:
                # printf/echo writers are builtins whose output fits the buffer.
                if re.match(r"\s*(printf|echo)\b", stripped) or "printf '%s'" in line:
                    continue
                offenders.append(f"{path.name}:{n}: {stripped}")
    assert not offenders, "unguarded early-exit pipelines:\n" + "\n".join(offenders)


def _extract(shell_func: str, text: str = VPS_CHECK) -> str:
    """The function's source, from `name()` to the line that closes it."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{shell_func}()"))
    end = next(i for i in range(start, len(lines)) if lines[i].rstrip() == "}")
    return "\n".join(lines[start : end + 1])


def _fake_show(tmp_path: Path, *, cat: str = "", **props: str) -> Path:
    """A `systemctl` answering `cat` and `show -p NAME --value`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    cases = "\n".join(f'    {k}) echo "{v}";;' for k, v in props.items())
    (bindir / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = cat ]; then cat <<"EOF"\n{cat}\nEOF\n  exit 0; fi\n'
        'if [ "$1" = show ]; then\n'
        '  for a in "$@"; do case "$a" in -p) ;; *) p="$a";; esac; done\n'
        '  for a in "$@"; do case "$a" in\n' + cases + "\n  esac; done; exit 0\nfi\n"
    )
    (bindir / "systemctl").chmod(0o755)
    return bindir


def _run(script: str, bindir: Path) -> str:
    r = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": "/tmp"},
        capture_output=True,
        text=True,
    )
    return r.stdout + r.stderr


def test_the_check_accepts_the_restart_policy_the_units_actually_ship(tmp_path):
    """`Restart=always` + `RestartPreventExitStatus=2` is the tested contract
    (R63: a refusal to start must stay down). The check demanded
    `Restart=on-failure` and so warned about every correctly-configured unit --
    noise that trains an operator to ignore the check."""
    unit = (ROOT / "deploy" / "ntx-soak.service").read_text()
    bindir = _fake_show(tmp_path, cat=unit)
    out = _run(
        "set -uo pipefail\n"
        'ok() { echo "[ok] $1"; }; warn() { echo "[warn] $1"; }\n'
        f"{_extract('bot_props')}\nbot_props ntx-soak.service\n",
        bindir,
    )
    assert "[warn]" not in out, out


def test_a_timer_that_runs_every_5_minutes_is_not_reported_as_5_days_away(tmp_path):
    """`NextElapseUSecMonotonic` counts from BOOT. Printed raw, the 5-minute
    alert timer read "next in 5d 18h" on a box that had been up 5d 18h -- the
    one number an operator uses to tell a live timer from a dead one."""
    bindir = _fake_show(
        tmp_path,
        NextElapseUSecRealtime="n/a",
        NextElapseUSecMonotonic="5d 18h 36min 30s",
    )
    script = (
        "set -uo pipefail\n"
        'cut() { if [ "$*" = "-d  -f1 /proc/uptime" ]; then echo 498892; else command cut "$@"; fi; }\n'
        f"{_extract('_elapse_seconds')}\n{_extract('timer_next')}\n"
        "timer_next ntx-alert.timer\n"
    )
    out = _run(script.replace("/proc/uptime", "/tmp/ntx-fake-uptime"), bindir)
    Path("/tmp/ntx-fake-uptime").write_text("498892.65 900000.00\n")
    out = _run(script.replace("/proc/uptime", "/tmp/ntx-fake-uptime"), bindir)
    assert "5d" not in out, out
    assert "in 1min" in out or "in 90s" in out or "in 1min 30s" in out, out


# --------------------------------------------------------- live deploy ----- #
def test_the_live_deploy_target_runs_the_whole_script_as_the_bot_user():
    """`sudo -n -u ntx test -x X && X` binds the && OUTSIDE sudo: only `test -x`
    ran as ntx and the script itself ran as the admin user, who cannot read
    /home/ntx (mode 750). The target could never have worked, which means there
    was no way to ship a fix to a running live node."""
    makefile = (ROOT / "Makefile").read_text()
    body = makefile.split("deploy-live:")[1].split("\ndeploy-dry:")[0]
    cmd = next(
        ln for ln in body.splitlines() if "deploy-live.sh" in ln and not ln.strip().startswith("@#")
    )
    assert "&&" not in cmd, f"the && splits the sudo: {cmd.strip()}"
    assert "sudo -n -u ntx /home/ntx/ntx/deploy/deploy-live.sh" in cmd


def test_every_script_the_admin_runs_as_the_bot_user_is_in_the_sudoers_file():
    """A Makefile target that shells out via `sudo -u ntx` to a script the
    sudoers file does not list just asks for a password and hangs."""
    sudoers = (ROOT / "deploy" / "sudoers-ntx-deploy").read_text()
    granted = {w.strip().rstrip(",") for w in sudoers.split() if w.startswith("/home/ntx/")}
    makefile = (ROOT / "Makefile").read_text()
    used = set()
    for line in makefile.splitlines():
        if "sudo -n -u ntx" in line:
            for word in line.split():
                if word.startswith("/home/ntx/") and word.endswith(".sh"):
                    used.add(word)
    missing = used - granted
    assert not missing, f"not granted in sudoers-ntx-deploy: {sorted(missing)}"


def test_the_deployer_clears_a_failed_state_before_starting():
    """A unit at StartLimitBurst refuses every later start until the failure is
    cleared. A deploy is a stop+start, so a run of deploys inside
    StartLimitIntervalSec locks the bot out of its own restart -- twice on
    2026-09-24, each time leaving the soak down until a human intervened."""
    for block in ("rollback()", 'for u in $STOPPED; do\n  log "starting $u"'):
        assert block in UPDATE, block
        body = UPDATE.split(block)[1][:1200]
        start = body.index("systemctl start")
        assert "reset-failed" in body[:start], f"{block} starts without clearing a failed state"


def test_a_trading_bot_never_gives_up_restarting():
    """Owner decision 2026-11-04 (after F084): a start limit turned a 19-minute glitch
    into a 38-hour live outage. Trading units retry forever, slowly; a refusal (exit 2)
    still stays down (R73), and a crash loop is still paged -- by the alerter's
    stale-snapshot check, since a crashing node writes no snapshot."""
    for name in ("ntx-s003.service", "ntx-soak.service", "ntx-s004-demo2.service"):
        lines = (ROOT / "deploy" / name).read_text().splitlines()
        val = {
            ln.split("=", 1)[0]: ln.split("=", 1)[1]
            for ln in lines
            if "=" in ln and not ln.startswith("#")
        }
        assert val.get("StartLimitIntervalSec") == "0", f"{name}: systemd may give up"
        assert int(val["RestartSec"]) >= 60, f"{name}: retries too fast"
        assert "2" in val.get("RestartPreventExitStatus", ""), f"{name}: a refusal must stay down"


# ------------------------------------------- two checkouts, one data dir ---- #
# docs/24 §1b. Both bots used to run from /home/ntx/ntx, so update.sh had
# to refuse every commit while the live unit was up -- and that froze continuous
# deployment for the DEMO soak too: 443 refused timer ticks on 2026-09-24/25,
# once a minute, in the deployer's log.
SETUP_SH = ROOT / "scripts" / "setup_live_checkout.sh"
LIVE_UNIT_FILE = (ROOT / "deploy" / "ntx-s003.service").read_text()
SOAK_UNIT_FILE = (ROOT / "deploy" / "ntx-soak.service").read_text()


def _defaults(text: str) -> dict[str, str]:
    """The `NAME=${NTX_...:-/default}` pairs a script declares."""
    return dict(re.findall(r"^(\w+)=\$\{NTX_\w+:-([^}]+)\}", text, re.M))


@pytest.mark.parametrize(
    "text,name",
    [(UPDATE, "update.sh"), (LIVE, "deploy-live.sh"), (SETUP, "setup_live_checkout.sh")],
)
def test_the_two_checkouts_never_collide(text, name):
    """One typo here and a live deploy advances the soak, or the timer advances
    the money node. The paths are a contract, so every script states both."""
    d = _defaults(text)
    assert d.get("DEMO_REPO") == DEMO_REPO, f"{name}: DEMO_REPO is {d.get('DEMO_REPO')!r}"
    assert d.get("LIVE_REPO") == LIVE_REPO, f"{name}: LIVE_REPO is {d.get('LIVE_REPO')!r}"
    assert DEMO_REPO != LIVE_REPO
    # Nested checkouts would make each one's git status full of the other's files.
    assert not LIVE_REPO.startswith(DEMO_REPO + "/")
    assert not DEMO_REPO.startswith(LIVE_REPO + "/")


def test_the_live_unit_runs_from_the_live_checkout():
    """The whole point: the money node's code is not the code the timer moves."""
    props = dict(
        ln.split("=", 1) for ln in LIVE_UNIT_FILE.splitlines() if "=" in ln and ln[0].isupper()
    )
    assert props["WorkingDirectory"] == LIVE_REPO
    assert props["ExecStart"].startswith(f"{LIVE_REPO}/.venv/bin/python")
    assert "ntx.live.node --strategy S003" in props["ExecStart"]


def test_the_soak_unit_still_runs_from_the_demo_checkout():
    """The half of the split that is easy to forget: only ONE unit moved."""
    props = dict(
        ln.split("=", 1) for ln in SOAK_UNIT_FILE.splitlines() if "=" in ln and ln[0].isupper()
    )
    assert props["WorkingDirectory"] == DEMO_REPO
    assert props["ExecStart"].startswith(f"{DEMO_REPO}/.venv/bin/python")


def test_both_bots_keep_one_shared_data_directory():
    """R29/R56: one journal, one set of state snapshots, one lock directory and
    one LIVE_ARMED -- which on this box lives under the data dir, because
    /etc/ntx does not exist (measured 2026-09-25). A live node pointed at the
    live checkout's own data/ would come up silently disarmed with an empty
    journal, and the monitor would show nothing."""
    assert f"Environment=NTX_DATA_DIR={DEMO_REPO}/data" in LIVE_UNIT_FILE
    # and the deployer writes its markers there, not next to whichever checkout
    # it happens to be advancing -- ntx.obs.alerts reads exactly one path.
    assert 'DATA=$(resolve "${NTX_DATA_DIR:-$DEMO_REPO/data}")' in UPDATE
    assert 'MAINTENANCE="$DATA/.deploy-in-progress"' in UPDATE
    assert "$REPO/data" not in _code(UPDATE), "a marker under the live checkout is invisible"


def test_the_live_unit_fails_loudly_if_the_shared_layout_is_missing():
    """R70: ConditionPathExists SKIPS the start and reports success. For the unit
    that trades real money the answer to 'my .env symlink is gone' must be a
    failed unit and an alert, not a quiet no-op."""
    assert f"AssertPathExists={LIVE_REPO}/.env" in LIVE_UNIT_FILE
    assert f"AssertPathIsDirectory={DEMO_REPO}/data" in LIVE_UNIT_FILE
    assert "ConditionPath" not in _code(LIVE_UNIT_FILE)


def test_the_live_unit_does_not_hand_the_env_file_to_systemd():
    """Measured on the box 2026-09-25: four risk parameters in .env carry an
    inline `# ...` comment, which systemd keeps as part of the value (it honours
    `#` only at the start of a line), and both API passphrases contain characters
    its EnvironmentFile parser treats as quoting. NTX_RISK_EQUITY_PCT would reach
    pydantic as unparseable text and the node would refuse to start; the
    passphrase would reach OKX corrupted. The .env is read by pydantic, through
    the checkout's own .env -- which in the live checkout is a symlink."""
    assert "EnvironmentFile" not in _code(LIVE_UNIT_FILE)
    assert ".env" in SETUP, "then something must create that symlink"


def test_the_venue_pin_still_wins_over_anything_read_from_env():
    """The unit pins the venue because .env holds BOTH keys (R56)."""
    assert "Environment=NTX_VENUE=okx_live" in LIVE_UNIT_FILE
    assert "Environment=NTX_VENUE=okx_demo" in SOAK_UNIT_FILE


# --------------------------------------------- update.sh: demo checkout only - #
def _update_prologue(tmp_path: Path) -> Path:
    """The shipped role-detection prologue in a throwaway directory.

    Cut at the first line that would do real work, so the test exercises the
    actual code that ships without letting it fetch, merge or restart anything.
    """
    d = tmp_path / "deploy"
    d.mkdir(parents=True, exist_ok=True)
    script = d / "update.sh"
    script.write_text(
        UPDATE[: UPDATE.index("UV=$(command -v uv")]
        + '\necho "PROCEEDED role=$ROLE units=[$MANAGED_UNITS] data=$DATA"\n'
    )
    script.chmod(0o755)
    return script


def _run_prologue(script: Path, **env: str) -> subprocess.CompletedProcess:
    import os

    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(script.parent), **env},
    )


def test_update_refuses_to_advance_the_live_checkout(tmp_path):
    """The one refusal that is still right. Nothing automatic may move the code
    the live node reloads on its next crash-restart (R14/F044)."""
    script = _update_prologue(tmp_path)
    r = _run_prologue(script, NTX_LIVE_REPO=str(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "LIVE checkout" in r.stdout
    assert "PROCEEDED" not in r.stdout, "it must stop before it so much as fetches"


def test_update_deploys_the_demo_checkout_without_asking_about_live(tmp_path):
    """The positive case, which is the whole bug: a timer tick in the demo
    checkout proceeds no matter what the live unit is doing (R70 -- a refusal
    proven only in the negative is not proven)."""
    script = _update_prologue(tmp_path)
    r = _run_prologue(script, NTX_LIVE_REPO=str(tmp_path / "elsewhere"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "role=demo" in r.stdout
    assert "ntx-soak.service" in r.stdout and "ntx-s003.service" not in r.stdout
    assert "REFUSING" not in r.stdout


def test_only_the_live_deploy_may_open_the_live_checkout(tmp_path):
    """And when it does, it manages no units: starting and stopping the live node
    is the admin's, not the bot user's (L2-01)."""
    script = _update_prologue(tmp_path)
    r = _run_prologue(script, NTX_LIVE_REPO=str(tmp_path), NTX_DEPLOY_LIVE_CHECKOUT="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "role=live" in r.stdout and "units=[]" in r.stdout
    # and that switch is set in exactly one place in the repo
    assert "NTX_DEPLOY_LIVE_CHECKOUT=1" in LIVE
    assert _code(UPDATE).count("NTX_DEPLOY_LIVE_CHECKOUT") == 1


def test_the_deploy_markers_live_in_the_shared_data_dir(tmp_path):
    """Not next to the checkout being deployed: the alerter, the monitor and the
    journal backup all look in one place."""
    script = _update_prologue(tmp_path)
    r = _run_prologue(
        script,
        NTX_LIVE_REPO=str(tmp_path),
        NTX_DEMO_REPO="/home/ntx/ntx",
        NTX_DEPLOY_LIVE_CHECKOUT="1",
    )
    assert "data=/home/ntx/ntx/data" in r.stdout, r.stdout


def test_the_journal_backup_runs_from_the_checkout_that_owns_the_data():
    """scripts/journal_backup.sh resolves data/ relative to ITSELF. Run from the
    live checkout it would find no journals and back up nothing, silently."""
    line = next(ln for ln in UPDATE.splitlines() if "journal_backup.sh" in ln)
    assert "$DEMO_REPO/scripts/journal_backup.sh" in line, line


# ------------------------------------------ deploy-live.sh: live checkout --- #
def test_the_live_deploy_only_ever_writes_to_the_live_checkout():
    """It may READ the demo checkout -- `status` compares the two -- but every
    command that moves a working tree or a ref must name the live one."""
    code = _shell_code(LIVE)
    mutating = ("fetch", "merge", "reset", "clone", "checkout", "pull")
    for repo, verb in re.findall(r"git -C \"\$(\w+)\" ([a-z-]+)", code):
        if verb in mutating:
            assert repo == "LIVE_REPO", f"deploy-live.sh runs `git {verb}` in ${repo}"
    assert 'PY="$LIVE_REPO/.venv/bin/python"' in code, "it must test the venv that will run"
    assert "$DEMO_REPO/deploy/update.sh" not in code, "that would deploy the soak instead"


def test_the_live_deploy_still_runs_as_the_bot_user_from_the_granted_path():
    """deploy-live.sh stays in the DEMO checkout: the sudoers grant names
    /home/ntx/ntx/deploy/deploy-live.sh with no wildcard, and a script that moved
    would just prompt for a password and hang."""
    sudoers = (ROOT / "deploy" / "sudoers-ntx-deploy").read_text()
    assert f"{DEMO_REPO}/deploy/deploy-live.sh" in sudoers
    assert f"{LIVE_REPO}/deploy/deploy-live.sh" not in sudoers


def test_the_live_deploy_never_starts_or_stops_the_live_unit():
    """L2-01: the account a pushed commit runs as must not reach the live
    switches, so this script only ever WAITS for the admin's stop."""
    code = _shell_code(LIVE)
    assert "systemctl stop" not in code and "systemctl start" not in code
    assert "sudo" not in code, "this script has no privileges and must not ask for any"
    assert "systemctl is-active" in code, "but it must confirm the stop happened"


# --------------------------------------------------- the migration script --- #
def test_the_setup_script_refuses_while_the_live_unit_is_active(tmp_path):
    """Creating the checkout its unit will point at, while it trades, is how you
    get a node whose code on disk is not the code it is running."""
    import os

    bindir = _fake_show(tmp_path, ActiveState="active")
    r = subprocess.run(
        ["bash", str(SETUP_SH)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "NTX_USER": os.environ.get("USER", ""),
            "NTX_DEMO_REPO": str(tmp_path / "demo"),
            "NTX_LIVE_REPO": str(tmp_path / "live"),
        },
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSING" in r.stdout and "is up" in r.stdout
    assert not (tmp_path / "live").exists(), "it must refuse before creating anything"


def _demo_checkout(tmp_path: Path) -> Path:
    demo = tmp_path / "demo"
    (demo / "src").mkdir(parents=True)
    (demo / "src" / "x.py").write_text("x = 1\n")
    (demo / ".env").write_text("NTX_VENUE=okx_live\n")
    # The lines of the real .gitignore this layout depends on. `/data/` and
    # friends match DIRECTORIES only, so a symlink of the same name is untracked
    # unless the setup script excludes it; `.env` has no trailing slash, so its
    # symlink is ignored like the file was.
    (demo / ".gitignore").write_text(".env\n/data/\n/logs/\n/reports/\n")
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "remote", "add", "origin", "git@example.invalid:example/project.git"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "init"],
    ):
        subprocess.run(cmd, cwd=demo, check=True, capture_output=True)
    return demo


def _fake_uv(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "uv").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bindir / "uv").chmod(0o755)
    return bindir


def test_the_setup_script_is_idempotent_and_never_moves_the_live_head(tmp_path):
    """It is a migration, so it will be re-run -- after a failure, or by someone
    checking. A second run must not reset the live checkout onto whatever the
    soak has moved to since: only `make deploy-live` may move that HEAD."""
    import os

    demo = _demo_checkout(tmp_path)
    live = tmp_path / "live"
    bindir = _fake_show(tmp_path, ActiveState="inactive")
    (bindir / "uv").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bindir / "uv").chmod(0o755)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "NTX_USER": subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip(),
        "NTX_DEMO_REPO": str(demo),
        "NTX_LIVE_REPO": str(live),
        "NTX_SETUP_VERIFY": "0",
    }
    first = subprocess.run(["bash", str(SETUP_SH)], capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    head = subprocess.run(
        ["git", "-C", str(live), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout
    assert head, first.stdout
    for name in (".env", "data", "logs", "reports"):
        assert (live / name).is_symlink(), name
        assert (live / name).resolve() == (demo / name).resolve(), name
    # the live checkout's git status must be clean -- .gitignore's /data/ and
    # /logs/ match directories only, so the symlinks would show up as untracked
    status = subprocess.run(
        ["git", "-C", str(live), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert status == "", f"untracked leftovers in the live checkout: {status!r}"
    assert "VERIFICATION SKIPPED" in first.stdout, "a skipped check must say so (R70)"

    # the soak moves on; the live checkout must not follow it
    (demo / "src" / "x.py").write_text("x = 2\n")
    subprocess.run(["git", "add", "-A"], cwd=demo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "later"], cwd=demo, check=True, capture_output=True)
    second = subprocess.run(["bash", str(SETUP_SH)], capture_output=True, text=True, env=env)
    assert second.returncode == 0, second.stdout + second.stderr
    head2 = subprocess.run(
        ["git", "-C", str(live), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout
    assert head2 == head, "a re-run moved the live checkout's HEAD"
    assert (live / "src" / "x.py").read_text() == "x = 1\n"


def test_the_setup_script_cannot_move_the_live_checkout_at_all():
    """Belt and braces on the test above: the commands that would do it are not
    in the script, so no future edit can reintroduce them quietly."""
    code = _shell_code(SETUP)
    for forbidden in ("reset --hard", "git merge", "git pull", "rm -rf", "git checkout"):
        assert forbidden not in code, forbidden


def test_every_script_the_admin_runs_as_the_bot_user_can_actually_run():
    """A `sudo -u ntx <script>` that sudoers does not list asks for a password.
    Over a plain `ssh` (no tty) that hangs forever, which is how a deploy target
    that had never worked went unnoticed for weeks (L2-05)."""
    sudoers = (ROOT / "deploy" / "sudoers-ntx-deploy").read_text()
    granted = {w.strip().rstrip(",") for w in sudoers.split() if w.startswith("/home/ntx/")}
    makefile = (ROOT / "Makefile").read_text()
    for line in makefile.splitlines():
        if "sudo -u ntx" not in line and "sudo -n -u ntx" not in line:
            continue
        scripts = [w for w in line.split() if w.startswith("/home/ntx/") and w.endswith(".sh")]
        if not scripts:
            continue
        passwordless = "sudo -n -u ntx" in line
        target = _make_target_of(makefile, line)
        if passwordless:
            assert set(scripts) <= granted, f"{target}: not in sudoers: {scripts}"
        else:
            assert "ssh -t " in _recipe(makefile, target), (
                f"{target} runs {scripts} with a password prompt but no tty"
            )


def _make_target_of(makefile: str, needle: str) -> str:
    target = "?"
    for line in makefile.splitlines():
        if line and not line[0].isspace() and ":" in line:
            target = line.split(":")[0]
        if line == needle:
            return target
    return target


def _recipe(makefile: str, target: str) -> str:
    out, keep = "", False
    for line in makefile.splitlines():
        if line.startswith(f"{target}:"):
            keep = True
        elif keep and line and not line[0].isspace():
            break
        if keep:
            out += line + "\n"
    return out


# ------------------------------------------------------------ the monitor --- #
def test_the_monitor_is_a_service_not_a_remembered_command():
    """It was started by hand over SSH and died with the session every time --
    the owner reported "the UI is down" four times in two days, and on each
    occasion the bots were healthy and only this process was missing."""
    unit = ROOT / "deploy" / "ntx-ui.service"
    assert unit.exists(), "the dashboard has no unit, so it dies with whatever started it"
    text = unit.read_text()
    assert "Restart=always" in text
    assert "WantedBy=multi-user.target" in text, "must come back after a reboot"
    assert "User=ntx" in text


def test_the_monitor_binds_loopback_only():
    """It is reached through an SSH tunnel. It shows balances and positions, and
    the box is shared with another project."""
    unit = (ROOT / "deploy" / "ntx-ui.service").read_text()
    assert "--host" not in unit, "binding beyond loopback must be a deliberate edit"
    main = (ROOT / "src" / "ntx" / "ui" / "__main__.py").read_text()
    assert "127.0.0.1" in main


def test_the_monitor_never_gives_up_restarting():
    """A start limit turned a transient port clash into sixteen hours of down.

    Installed 2026-09-26 16:43 while the hand-started monitor still held 8788:
    ten failed binds in 100 s tripped `StartLimitBurst`, systemd stopped trying,
    and the unit was `failed` until somebody looked. The dashboard appeared fine
    the whole time because the stray process it was meant to replace was serving
    it -- so the one symptom that would have revealed it was hidden by the very
    bug it was replacing.

    Nothing about a read-only dashboard justifies giving up (F065 sweep).
    """
    unit = (ROOT / "deploy" / "ntx-ui.service").read_text()
    assert "StartLimitIntervalSec=0" in unit, "the monitor must retry forever"
    assert "StartLimitBurst" not in unit, "a burst limit with no interval still stops the retries"
    assert "Restart=always" in unit


def test_the_tunnel_does_not_start_its_own_monitor():
    """`make ui-vps` used to BE the monitor, which is why the UI kept dying.

    It ssh'd in, killed whatever held 8788, and ran uvicorn in the foreground of
    that session -- so the dashboard lived exactly as long as the terminal, and
    the owner reported "the UI is down" four times in two days while every bot
    was healthy.

    Making the monitor a service (ntx-ui.service) only half-fixed it: this target
    would still kill the service to take the port, and hand it back dead on
    Ctrl-C. Worse, on 2026-09-27 the service could not start AT ALL because a
    copy started this way still held 8788, so it burned its start limit and sat
    `failed` for sixteen hours -- invisible, because the stray process was
    serving the page.

    A tunnel forwards a port. It does not own the thing on the other end.
    """
    mk = (ROOT / "Makefile").read_text()
    target = mk[mk.index("ui-vps:") :]
    target = target[: target.index("\nui-snapshot:")]
    # Comments explain the history and name both; the commands may do neither.
    commands = "\n".join(
        ln for ln in target.splitlines() if not ln.strip().lstrip("@").startswith("#")
    )
    assert "-m ntx.ui" not in commands, "the tunnel must not start a monitor of its own"
    assert "kill" not in commands, "the tunnel must not kill what is serving the port"
    assert "-L 8788:127.0.0.1:8788" in commands, "it is still a tunnel"


# ---- installing unit files is a step, not a hope (2026-09-28) -------------- #

INSTALL_UNITS = (ROOT / "scripts" / "install_units.sh").read_text()


def test_the_unit_installer_never_restarts_a_trading_node():
    """Installing a unit file changes the NEXT start. It must not cause one.

    R57: the live node is never restarted by automation; deploy-live.sh is the
    only path to that.
    """
    commands = "\n".join(ln for ln in INSTALL_UNITS.splitlines() if not ln.strip().startswith("#"))
    for forbidden in ("systemctl restart", "systemctl stop", "systemctl start ntx-s003"):
        assert forbidden not in commands, f"the installer must not run `{forbidden}`"


def test_the_installer_refuses_to_arm_the_live_unit_before_the_migration():
    """ntx-s003.service asserts $LIVE_REPO exists; installing it early arms a
    failure at the money node's next start."""
    assert 'name" = "ntx-s003.service" ] && [ ! -d "$LIVE_REPO/.git"' in INSTALL_UNITS


def test_the_installer_clears_a_failed_unit():
    """A unit that could not start keeps its start-limit counter across a
    reinstall, so the corrected file never gets a chance (the 2026-09-26 UI)."""
    assert "reset-failed" in INSTALL_UNITS


def test_the_installer_only_touches_what_differs():
    assert "cmp -s" in INSTALL_UNITS
    assert 'install -m 644 "${changed[@]}"' in INSTALL_UNITS


def test_the_installer_has_a_dry_run():
    assert "--dry-run" in INSTALL_UNITS


def test_the_drift_message_points_at_the_installer():
    """The deploy told operators to paste a command. It now names the script."""
    assert "install_units.sh" in UPDATE, (
        "update.sh must point at the installer, not at a command to retype"
    )


def test_the_deploy_restarts_the_dashboard():
    """New code on disk is not new code in a long-lived python process.

    The monitor kept serving the previous commit after every deploy -- a picker
    fix shipped at 09:47 was still missing at 10:05, because the process had
    imported the old modules at start. It is read-only, loopback-only and holds
    no position, so restarting it costs nothing.
    """
    assert "systemctl restart ntx-ui.service" in UPDATE
    # It must not be able to fail the deploy or trigger a rollback: the bots
    # matter, the dashboard does not.
    tail = UPDATE[UPDATE.index("systemctl restart ntx-ui.service") :]
    assert "rollback" not in tail[:400], "a dashboard restart must never roll back a deploy"


def test_the_dashboard_restart_never_touches_a_trading_unit():
    tail = UPDATE[UPDATE.index("systemctl restart ntx-ui.service") :][:400]
    for unit in ("ntx-s003", "ntx-soak", "ntx-s004"):
        assert unit not in tail


def test_the_deploy_account_may_restart_only_demo_bots_and_the_dashboard():
    """A grant is a capability. Money units are not in it (R14, R57)."""
    sudoers = (ROOT / "deploy" / "sudoers-ntx-deploy").read_text()
    granted = [ln for ln in sudoers.splitlines() if ln.startswith("ntx ALL=(root)")]
    assert any("restart ntx-ui.service" in ln for ln in granted), (
        "the deploy cannot restart the dashboard it is expected to keep current"
    )
    for ln in granted:
        # Demo bots are granted on purpose (F072): they are deployed and
        # health-checked like the soak. The money node never is (R57).
        assert "ntx-s003" not in ln, "the live node is never restarted by automation (R57)"
