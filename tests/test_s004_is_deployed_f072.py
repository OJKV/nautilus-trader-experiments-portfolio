"""F072: a deployed bot that no deploy ever updates.

`update.sh` restarts `MANAGED_UNITS` after a successful pull and test run. That
list held exactly one entry -- `ntx-soak.service` -- while three bots run on the
box. `ntx-s004-demo2.service` was started once and then ran whatever code it had
at that moment, for as long as the process lived.

Caught 2026-10-06, by its own alarm text. S004 was still printing the pre-2026-10-05
wording:

    WATCHDOG: the venue holds 4.6 AVAX-USDT-SWAP that this node does not manage -- no ...

rather than the severity-aware message shipped that day. Its unit had started
14:49:33; the deploy landed 15:08:16. Nineteen minutes, and it would have stayed
behind until the next reboot -- indefinitely, in principle.

Why it matters beyond a stale log line: S004 shares the live code path. Every
safety fix in `strategies/base.py` -- the venue confirmation before a close
(F069), the retry-then-degrade guard (F068), the phantom accounting (F070) --
reaches it only when something restarts it. A bot silently running last month's
execution code is the kind of thing that is only ever discovered afterwards.

It is a demo bot on its own sub-account, so restarting it is exactly as safe as
restarting the soak, and it goes through the same gate: pull, run the suite on the
box, restart, health-check the snapshot, roll back if that fails (R57).

Written before the fix (R41).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATE = (ROOT / "deploy" / "update.sh").read_text()


def test_every_demo_bot_is_restarted_by_the_deploy():
    line = next(ln for ln in UPDATE.splitlines() if "MANAGED_UNITS=" in ln and "soak" in ln)
    assert "ntx-s004-demo2.service" in line, (
        "a bot no deploy restarts runs stale code until something reboots the box"
    )


def test_the_live_unit_is_still_never_managed_here():
    """R57/L2-01: the deployer must not be able to name the money node."""
    empty = next(ln for ln in UPDATE.splitlines() if 'MANAGED_UNITS=""' in ln)
    assert empty, "the live role still manages nothing"
    soak_line = next(ln for ln in UPDATE.splitlines() if "MANAGED_UNITS=" in ln and "soak" in ln)
    assert "ntx-s003.service" not in soak_line


def test_the_new_unit_has_a_health_check_mapping():
    """Restarting without health-checking is how a deploy reports success on a
    node that came back broken (R57)."""
    block = UPDATE[UPDATE.index("snapshot_args()") :][:500]
    assert "ntx-s004-demo2.service" in block, "a restart with no snapshot check is not verified"
    assert "S004-demo.json" in block


def test_the_deploy_account_can_actually_restart_what_it_manages():
    """A unit in MANAGED_UNITS with no grant is a deploy that reports success
    while changing nothing."""
    sudoers = (ROOT / "deploy" / "sudoers-ntx-deploy").read_text()
    managed = next(ln for ln in UPDATE.splitlines() if "MANAGED_UNITS=" in ln and "soak" in ln)
    units = [w for w in managed.split('"')[1].split() if w.endswith(".service")]
    assert units, "sanity: the list is not empty"
    for unit in units:
        for verb in ("stop", "start"):
            assert f"systemctl {verb} {unit}" in sudoers, f"no grant to {verb} {unit}"


def test_no_grant_reaches_the_money_node():
    sudoers = (ROOT / "deploy" / "sudoers-ntx-deploy").read_text()
    granted = [ln for ln in sudoers.splitlines() if ln.startswith("ntx ALL=(root)")]
    for ln in granted:
        assert "ntx-s003.service" not in ln, "the live node is never restarted by automation (R57)"


def test_a_unit_we_cannot_stop_does_not_roll_back_the_whole_deploy():
    """`set -e` + `trap rollback ERR` made one missing grant everyone's problem.

    Adding a unit to MANAGED_UNITS before its sudoers line is installed would
    have failed the stop, tripped the ERR trap, and rolled back the deploy for
    every other bot. A guard may refuse an action; it may not refuse to run (R73).
    """
    block = UPDATE[UPDATE.index('log "stopping $u"') :][:900]
    assert 'if ! sudo -n systemctl stop "$u"; then' in block
    assert "continue" in block, "skip that unit, do not abort the deploy"
    assert "running OLD CODE" in block, "silence here is how stale code survives"


def test_the_skip_is_alerted_not_just_logged():
    block = UPDATE[UPDATE.index('log "stopping $u"') :][:900]
    assert "alert " in block or "notice " in block, "a bot stuck on old code must reach a human"
