"""The deploy must notice a MISSING GRANT before it needs one.

CORRECTED 2026-10-06, same day: the first version of this check compared
`deploy/sudoers-ntx-deploy` against `/etc/sudoers.d/ntx-deploy` with `cmp`. That
file is `0440 root:root` and the deploy runs as `ntx`, which cannot read it -- so
the comparison failed on permission every single time and the check reported
drift forever, including immediately after a correct install. A check that always
fires is worse than no check (R70), and I shipped one.

The thing worth checking was never the file's bytes. It is the **capability**:
can this account restart the units it is told to manage? `sudo -n -l` answers
that, needs no privilege to run, and cannot cry wolf.

2026-10-06: S004 was added to the deploy's restart list in the same commit series
as its sudoers grant. The grant is a root-owned file in /etc/sudoers.d/ and the
deploy runs as the bot user, so the code shipped and the permission did not. The
deploy then stopped the soak, failed on S004 with `sudo: a password is required`,
and the ERR trap rolled it back -- every 65 seconds, with the soak down in between.

The permission file itself can never be installed automatically, and that is not
an oversight: anything that can write `deploy/sudoers-ntx-deploy` in the repo
would otherwise be granting itself root on the box. It stays a human action.

What should not stay human is *finding out*. The deploy already compares the
systemd units in git against the installed ones and names the command to fix a
difference. The sudoers file is exactly the same kind of artefact and was simply
never checked -- so the first sign of a stale one was a broken pipeline rather
than a line in the log.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATE = (ROOT / "deploy" / "update.sh").read_text()
# The function body, not the first mention of the filename anywhere in the script.
CHECK = UPDATE[UPDATE.index("check_sudoers_drift() {") :]
CHECK = CHECK[: CHECK.index("\ncheck_unit_drift()")]
# Comments explain the history and name both the file and the old mistake; the
# COMMANDS may do neither.
CMDS = "\n".join(ln for ln in CHECK.splitlines() if not ln.strip().startswith("#"))


def test_the_deploy_checks_the_grant_it_depends_on():
    assert "sudo -n -l" in CHECK, (
        "test the capability, not a root-owned file this account cannot read"
    )


def test_it_never_reads_the_installed_sudoers_file():
    """0440 root:root. Reading it as `ntx` fails, and a failed read is not drift."""
    assert "cmp -s" not in CMDS
    reads = [ln for ln in CMDS.splitlines() if "/etc/sudoers.d/" in ln and "log " not in ln]
    assert reads == [], f"the installed file must only ever be NAMED, never read: {reads}"


def test_it_checks_every_unit_it_manages():
    assert "$MANAGED_UNITS" in CHECK, "a grant for one unit says nothing about another"


def test_it_names_the_exact_command_a_human_must_run():
    assert "/etc/sudoers.d/ntx-deploy" in CHECK
    assert "visudo -c" in CHECK, "a bad sudoers file can lock everyone out; verify it"


def test_it_alerts_rather_than_only_logging():
    assert "alert " in CHECK or "notice " in CHECK


def test_it_never_tries_to_install_it():
    """Self-granting root from a repo file is the one thing this must not do."""
    assert "install -m 440" not in CMDS.split("log ")[0], (
        "the deploy may report the gap; it may never apply it"
    )


def test_a_missing_grant_names_the_unit_it_is_missing_for():
    assert "$u" in CHECK
