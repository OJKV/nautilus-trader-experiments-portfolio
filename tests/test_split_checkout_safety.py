"""The split must not break the live node before the migration has happened.

`deploy/ntx-s003.service` now points at /home/ntx/ntx-live and asserts that path
exists. Until the live checkout is created, installing the repo's unit files
would make the money node fail to start at its next restart or reboot -- and
`check_unit_drift` prints exactly that install command, on every demo deploy,
with an alert attached.

So the drift check must not advertise the live unit before the live checkout
exists, and the migration must be a deliberate step rather than something an
operator is nudged into by an alert they were told to expect.

Second half: `verify` must be able to tell WHICH checkout the running live node
is using. A healthy snapshot proves neither -- the snapshot lives in the shared
data directory and is written whichever checkout the node runs from. Without this
`make deploy-live` prints "live deploy complete" while the money node is still
running the demo checkout, which the timer fast-forwards every minute. That is
the exact situation the split exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATE = (ROOT / "deploy" / "update.sh").read_text()
DEPLOY_LIVE = (ROOT / "deploy" / "deploy-live.sh").read_text()


def test_drift_check_skips_the_live_unit_until_the_live_checkout_exists():
    body = UPDATE.split("check_unit_drift()")[1].split("\n}")[0]
    assert "LIVE_REPO" in body, (
        "the drift check advertises installing ntx-s003.service even when "
        "/home/ntx/ntx-live does not exist, which would keep the money node down"
    )


def test_drift_check_names_the_files_rather_than_globbing():
    """The glob silently reinstalls every other unit too."""
    body = UPDATE.split("check_unit_drift()")[1].split("\n}")[0]
    assert "$changed" in body.split("a human must run")[1][:400], (
        "the remediation should name the changed files, not deploy/*.service"
    )


def test_the_rollback_trap_catches_signals_not_just_errors():
    """Ctrl-C or a dropped ssh during `sync` leaves the checkout on an untested
    commit, the unit stopped but enabled -- so the next start or reboot runs the
    money node on code whose test run never finished."""
    line = next(ln for ln in UPDATE.splitlines() if ln.strip().startswith("trap rollback"))
    for sig in ("INT", "TERM", "HUP"):
        assert sig in line, f"trap does not catch {sig}: {line.strip()}"


def test_verify_checks_which_checkout_the_live_unit_actually_runs():
    body = (
        DEPLOY_LIVE.split('PHASE" = verify')[-1]
        if 'PHASE" = verify' in DEPLOY_LIVE
        else DEPLOY_LIVE
    )
    assert "WorkingDirectory" in body or "ExecStart" in body, (
        "verify passes on a healthy snapshot alone; the snapshot is written into "
        "the SHARED data dir whichever checkout the node runs from, so it cannot "
        "distinguish a successful split from one that was never installed"
    )


def test_verify_requires_the_commit_to_have_been_tested_here():
    """write_stamp runs only after the suite passes, so the stamp is the evidence
    that this commit was tested in this checkout."""
    assert "deploy-verified" in DEPLOY_LIVE or "write_stamp" in DEPLOY_LIVE
