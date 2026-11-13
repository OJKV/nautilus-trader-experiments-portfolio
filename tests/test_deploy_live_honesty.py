"""`deploy-live.sh` must not report success when it deployed nothing.

Measured 2026-09-24 18:24 UTC, running the documented live-deploy procedure:

    18:24:16Z 2/3 pull, test on the box, install
    18:24:18Z updating ae792d84 -> f5c6ef00
    18:24:18Z the LIVE node is running and NTX_DEPLOY_RESTART_LIVE=never.
    18:24:18Z   commit f5c6ef00 is NOT being applied
    18:24:38Z live deploy complete: ae792d8          <-- a lie

Three faults, each on its own worth fixing:

1. The unit was stopped a second earlier and was still shutting down, so
   `update.sh` saw it as active and refused to apply anything. Stopping is not
   instant; the node takes ~10 s to close its clients.
2. `verify` only checked that the snapshot was healthy. The node WAS healthy --
   it was healthy running the old code. "The bot is up" and "the deploy
   happened" are different claims (R31).
3. The typed confirmation could not be automated: `sudo` strips the environment
   variable the script reads, so an operator with standing authorisation had no
   supported way to run it.

The result was a live node stopped and restarted for nothing, and a script that
said it worked.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "deploy" / "deploy-live.sh").read_text()


def test_sync_waits_for_the_unit_to_actually_stop():
    body = SCRIPT.split('if [ "$PHASE" = sync ]')[1]
    assert "is-active" in body, (
        "sync must confirm the live unit is really down; systemctl stop returns "
        "before the node has finished shutting down, and update.sh then refuses"
    )


def test_verify_fails_when_the_code_did_not_change():
    body = SCRIPT.split("3/3")[1] if "3/3" in SCRIPT else SCRIPT
    assert "origin/main" in body, (
        "verify must compare the checkout against origin/main; a healthy snapshot "
        "only proves the old code is running well"
    )


def test_the_confirmation_can_be_given_as_an_argument():
    """sudo strips the environment, so an env var is unusable through it."""
    assert "--yes" in SCRIPT


def test_the_confirmation_is_still_required_by_default():
    assert "DEPLOY LIVE" in SCRIPT
    assert "read -r" in SCRIPT


def test_every_phase_is_still_present():
    assert "check|sync|verify)" in SCRIPT
    for phase in ("check", "sync"):
        assert f'"$PHASE" = {phase}' in SCRIPT


UPDATE_SH = (ROOT / "deploy" / "update.sh").read_text()


def test_a_deliberately_stopped_live_unit_can_be_deployed_to():
    """The original fault: `live_present()` returned true when the unit was
    merely ENABLED -- which it always is after `systemctl enable --now` -- so the
    documented procedure (stop the live unit, run update.sh, start it) could
    never work. update.sh refused every time, *after* the node had been stopped
    and restarted for nothing.

    That whole mechanism is gone with the two-checkout split (docs/24 §1b):
    update.sh runs in the DEMO checkout and cannot reach the live node's code at
    all, so it no longer has to ask whether live is up. The live checkout has its
    own gate, and the claim "the unit is down" is still verified rather than
    trusted -- the failure being guarded against is the same one."""
    live = UPDATE_SH.split("live_unit_down()")[1].split("\n}")[0]
    assert "unit_down ntx-s003.service" in live, live
    # the check itself is shared with the demo units since L3-09
    body = UPDATE_SH.split("\nunit_down()")[1].split("\n}")[0]
    assert "ActiveState" in body, "the caller's claim must be checked against reality"
    assert "inactive|failed" in body
    assert "NTX_DEPLOY_LIVE_CHECKOUT" in UPDATE_SH, "no way to deploy to a stopped live unit"
    code = "\n".join(ln for ln in UPDATE_SH.splitlines() if not ln.strip().startswith("#"))
    assert "live_present" not in code, "the enabled-means-present check is obsolete"


def test_the_live_deploy_sets_that_flag():
    assert "NTX_DEPLOY_LIVE_CHECKOUT=1" in SCRIPT


# ---- the flatness gate has two doors, not one (2026-09-27) ---------------- #


def test_an_open_position_is_a_second_door_not_a_refusal():
    """S003 is flat 31.5% of the time, so "wait until flat" is a deadlock.

    A gate nobody can pass gets worked around, and the workaround -- editing the
    unit by hand, or skipping the script -- loses every other check with it.
    """
    assert 'CONFIRM_OPEN="DEPLOY OVER OPEN POSITIONS"' in SCRIPT
    assert "ntx.ops.restart_cost --live" in SCRIPT, (
        "the operator must be told what the restart costs before being asked"
    )


def test_the_second_door_asks_a_different_question():
    """Muscle memory is the threat: the same words would be typed unread."""
    assert 'NEED="$CONFIRM_OPEN"' in SCRIPT
    assert "Type $NEED to continue" in SCRIPT


def test_yes_does_not_open_the_second_door():
    """Standing authorisation covers the ordinary deploy, not this one."""
    branch = SCRIPT[SCRIPT.index('if [ "$NEED" = "$CONFIRM_OPEN" ]') :]
    assert branch.startswith('if [ "$NEED" = "$CONFIRM_OPEN" ] ||'), (
        "--yes must not skip the confirmation when a position is open"
    )


def test_an_unreadable_account_is_never_a_door():
    """R70: "I could not tell" must not read as "nothing is open"."""
    assert "the live account could not be read (exit $state)" in SCRIPT
    assert "Not knowing whether a position is open is not the same as there being none." in SCRIPT


def test_the_three_outcomes_stay_three():
    gate = SCRIPT[SCRIPT.index('"$PY" -m ntx.ops.venue_state --live') :]
    gate = gate[: gate.index("esac")]
    for arm in ("0)", "1)", "*)"):
        assert arm in gate, f"missing the {arm} arm of the flatness check"


# ---- the last gate before live could never pass (2026-09-29) -------------- #


def test_the_verified_stamp_is_compared_as_a_sha_not_as_a_line():
    """`write_stamp` writes "<sha> <unix-ts>". `verify` compared the whole line.

    Observed 2026-09-29 on a real live deploy:

        THIS COMMIT WAS NOT TESTED IN THIS CHECKOUT: stamp 9f59ea12, HEAD 9f59ea12

    Both sides identical, reported as a mismatch -- because the message prints
    `${VAR:0:8}` and the comparison used the full string, one of which carried a
    timestamp. So the final gate before real money failed every single time a
    stamp existed, and said something visibly self-contradictory while doing it.

    A check that cannot pass is not a check: it trains the operator to read
    "deploy failed" as "deploy fine" (R70).
    """
    assert "cut -d' ' -f1" in SCRIPT or "awk '{print $1}'" in SCRIPT, (
        "the stamp holds a sha AND a timestamp; compare the sha"
    )
    block = SCRIPT[SCRIPT.index("STAMPED=") :][:400]
    assert "$STAMP" in block
