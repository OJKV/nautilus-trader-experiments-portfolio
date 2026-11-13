#!/usr/bin/env bash
# Pull the new code onto the box, verify it HERE, restart what is safe to
# restart, and undo everything if any of that fails.
#
#   sudo -u ntx /home/ntx/ntx/deploy/update.sh            # the normal path
#   sudo -u ntx /home/ntx/ntx/deploy/update.sh --dry-run  # say what it would do
#
# It also runs itself from `ntx-update.timer`, which is what makes "push to
# GitHub" the whole deployment procedure.
#
# Borrowed from the sibling ARGUS bot, which paid for each of these:
#   * `uv` is resolved explicitly -- `sudo -u` does not load a login shell, so
#     PATH does not have it (ARGUS F-078).
#   * The sync flags match `make install` exactly; a plain `uv sync` prunes the
#     dev extras and deletes the tooling this script's own gate needs (F-082).
#   * The rollback trap is armed BEFORE the merge, not after (F-078).
#   * "Already up to date" still verifies the environment: a run that died
#     during the sync leaves a new checkout on old dependencies (F-081).
#   * If this script changed in the merge, the running copy is stale -- syntax
#     check the new one and re-exec it, or a broken update.sh pulls its own fix,
#     fails, and rolls it away forever (F-083).
#
# ntx-specific, because this bot is a long-running daemon holding leveraged
# positions rather than a timer-driven job:
#   * `ntx` is installed EDITABLE, so a `git pull` changes the code a running
#     node will import at its next lazy import (`write_state` imports
#     `ntx.obs.atomic` every 15 s). The node is therefore STOPPED BEFORE the
#     merge and started after the tests pass -- never pulled out from under.
#   * TWO CHECKOUTS, ONE DATA DIRECTORY. This script advances the DEMO checkout
#     (/home/ntx/ntx) and restarts the soak, and that is all it can reach. The
#     live node runs from /home/ntx/ntx-live, which only `make deploy-live`
#     moves. Until 2026-09-25 both bots shared one checkout, so this script had
#     to refuse EVERY commit while the live unit was up -- on 2026-09-24/25 that
#     froze the soak's deployment for 443 timer ticks (R14 was right; the shared
#     checkout was the bug). The refusal survives where it is still true: run
#     against the LIVE checkout, this script stops before it touches anything.
#   * Success means the node is writing a healthy snapshot again, not that
#     `systemctl` returned 0 (R31).
#   * Nothing here deletes anything under data/ (R29); the journal is backed up
#     before every deploy.
#   * Unit files live in /etc and a git pull cannot touch them: drift is
#     reported so a human runs the one `sudo install` line.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO=$(pwd -P)
BRANCH=${NTX_DEPLOY_BRANCH:-main}
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
# Two kinds of message, and they must not look alike on a phone. Until 2026-10-06
# every one went out as `service_failed` CRITICAL -- so "please install this
# file" paged exactly like a crashed trading node, and a deploy looping every
# 65 s would have paged ~55 times an hour. Both kinds are now deduplicated: the
# first goes out at once, an identical one repeats only after hours (R75).
#   alert  -- something actually went wrong (a rollback, a unit left down)
#   notice -- a human needs to do something, and nothing is broken yet
alert() {
  "$REPO/.venv/bin/python" -m ntx.obs.alerts --kind deploy_failed --severity critical \
    --message "$1" >/dev/null 2>&1 || true
}
notice() { "$REPO/.venv/bin/python" -m ntx.obs.alerts --kind deploy_action_needed --severity warning --message "$1" >/dev/null 2>&1 || true; }

# --- which checkout am I? ------------------------------------------------- #
# The two paths are a contract shared with deploy/deploy-live.sh,
# scripts/setup_live_checkout.sh and deploy/ntx-s003.service; tests/test_deploy.py
# asserts all four agree. The env overrides exist for those tests.
DEMO_REPO=${NTX_DEMO_REPO:-/home/ntx/ntx}
LIVE_REPO=${NTX_LIVE_REPO:-/home/ntx/ntx-live}
resolve() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
DEMO_REPO=$(resolve "$DEMO_REPO")
LIVE_REPO=$(resolve "$LIVE_REPO")
ROLE=demo
[ "$REPO" = "$LIVE_REPO" ] && ROLE=live

# The one refusal that is still true. Nothing automatic may advance the live
# checkout: swapping the code a live node reloads on its next crash-restart is a
# decision with money on it (R14), and a restart is an adoption cycle whose
# failure branch can flatten (F044). `deploy-live.sh sync` is the only caller
# allowed here, and only after a human typed DEPLOY LIVE over a flat account.
# This is the FIRST thing the script does: a run from the wrong directory must
# not so much as fetch.
if [ "$ROLE" = live ] && [ "${NTX_DEPLOY_LIVE_CHECKOUT:-}" != 1 ]; then
  log "REFUSING: $REPO is the LIVE checkout."
  log "  Advancing it changes the code the live node runs -- that is never automatic (R14)."
  log "  The timer deploys $DEMO_REPO only. To ship to live:  make deploy-live"
  exit 2
fi
# Units, and the files that are shared between the two bots, follow from the role.
# The data directory is SHARED on purpose (journals, locks, state snapshots and
# the arming file are namespaced per bot, R29/R56), so the maintenance marker the
# alerter looks for is written where the alerter reads it, whichever checkout is
# being deployed.
DATA=$(resolve "${NTX_DATA_DIR:-$DEMO_REPO/data}")
if [ "$ROLE" = live ]; then
  # This script never starts or stops the live unit and has no sudo grant to:
  # the admin account does that either side of `deploy-live.sh sync` (L2-01).
  MANAGED_UNITS=""
  SUFFIX="-live"
else
  # Every demo bot, not just the soak. S004 was absent until 2026-10-06 and so
  # ran whatever code it held at its last reboot -- caught only because its alarm
  # text was a day out of date (F072). It shares `strategies/base.py` with the
  # money node, so a safety fix that never reaches it is the worst kind of stale.
  MANAGED_UNITS="ntx-soak.service ntx-s004-demo2.service"
  SUFFIX=""
fi

UV=$(command -v uv || true)
[ -x "$UV" ] || UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || UV=/home/ntx/.local/bin/uv
if [ ! -x "$UV" ]; then
  log "uv not found (PATH, \$HOME/.local/bin, /home/ntx/.local/bin)"
  exit 1
fi

# Must match `make install` exactly, or the deploy installs a different
# environment from the one the tests were green in.
sync_env() { "$UV" sync --frozen --python 3.12 --all-extras --quiet; }

# A tick that finds no new commit must be nearly free: the timer runs every
# minute, and re-running `uv sync` + the full suite each time turned the trading
# box into a test treadmill (F051). The environment is still re-verified, but on
# a stamp: after a deploy, and at most every VERIFY_EVERY seconds after that.
# Per checkout: the two roles are at different commits by design, so one shared
# stamp would make each deploy look stale to the other.
STAMP="$DATA/.deploy-verified$SUFFIX"
VERIFY_EVERY=${NTX_DEPLOY_VERIFY_EVERY:-21600}   # 6 h

write_stamp() { printf '%s %s\n' "$(git rev-parse HEAD)" "$(date +%s)" > "$STAMP"; }

# Swapping the code stops the node for a couple of minutes, which is longer than
# the alerter's staleness limit -- so every deploy used to fire a critical and
# page the operator for routine work (F053). This marker excuses STALENESS only,
# never an unprotected position, and the alerter ignores it once it is stale
# itself, so a deploy that dies cannot silence anything.
# Shared, not per role: ntx.obs.alerts looks for exactly one path under the data
# directory, and a live deploy silences staleness for the same reason a demo one does.
MAINTENANCE="$DATA/.deploy-in-progress"
mark_maintenance() { printf '%s %s\n' "$(date +%s)" "$ROLE" > "$MAINTENANCE"; }  # role: O3-07
clear_maintenance() { rm -f "$MAINTENANCE"; }
trap clear_maintenance EXIT

stamp_is_fresh() {  # $1 = sha
  [ -f "$STAMP" ] || return 1
  local sha ts now
  read -r sha ts < "$STAMP" || return 1
  [ "$sha" = "$1" ] || return 1
  now=$(date +%s)
  [ $((now - ts)) -lt "$VERIFY_EVERY" ]
}

unit_down() {  # unit
  # `is-active` is not enough: a unit sits in `activating (auto-restart)` for the
  # whole RestartSec window after a crash, reads as not active, and comes back up
  # on its own in the middle of the merge and the `uv sync` (L2-02, L3-09).
  case "$(systemctl show -p ActiveState --value "$1" 2>/dev/null)" in
    inactive|failed|"") return 0 ;;
    *) return 1 ;;
  esac
}

units_active() {
  # Only ever the units this role owns. Before the checkout split this listed
  # ntx-s003.service too, and the only thing standing between a demo deploy and
  # `systemctl stop ntx-s003` was the live_present() refusal above it; now the
  # deployer cannot name the live unit at all, and the bot user has no grant for
  # it either (deploy/sudoers-ntx-deploy).
  # "Active" means anything but down: a bot waiting out its RestartSec is
  # stopped (which cancels the pending restart) and started after the tests.
  for u in $MANAGED_UNITS; do
    unit_down "$u" || echo "$u"
  done
  return 0
}

snapshot_args() {  # unit -> "<snapshot path> <--demo|--live>"
  case "$1" in
    ntx-soak.service) echo "$DATA/state/S003-demo.json --demo" ;;
    ntx-s004-demo2.service) echo "$DATA/state/S004-demo.json --demo" ;;
    ntx-s003.service) echo "$DATA/state/S003-live.json --live" ;;
  esac
}

live_unit_down() {
  # For the live role only, and it is the ADMIN's stop we are confirming, not
  # requesting.
  unit_down ntx-s003.service
}

# Touched just before every start. A snapshot under 90 s old is not proof the
# NEW process is healthy: a rollback that fails fast restarts within seconds of
# the stop, and the file the old process wrote just before it was stopped
# passed the age check while the new one never wrote anything (L2-11).
STARTED="$DATA/.deploy-started$SUFFIX"

wait_healthy() {  # unit
  local args snap deadline
  args=$(snapshot_args "$1")
  [ -n "$args" ] || return 0
  snap=${args%% *}
  deadline=$((SECONDS + ${NTX_DEPLOY_HEALTHY_S:-180}))
  while [ $SECONDS -lt $deadline ]; do
    # shellcheck disable=SC2086
    if [ "$snap" -nt "$STARTED" ] \
       && "$REPO/.venv/bin/python" -m ntx.ops.snapshot_check $args --max-age 90 >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  [ "$snap" -nt "$STARTED" ] || log "  $1 wrote no snapshot since it was started"
  # shellcheck disable=SC2086
  "$REPO/.venv/bin/python" -m ntx.ops.snapshot_check $args --max-age 90 || true
  return 1
}

# A commit whose deploy failed here is not retried on every tick. Without this,
# after a rollback HEAD=OLD and origin=NEW again, so every timer tick repeated
# stop -> merge -> sync -> suite -> rollback: both demo bots bounced every ~5
# minutes, each bounce a full adoption cycle, and R75's dedup reduced it to one
# push every 6 h (L2-08, L3-07). The stamp skips ONE sha; any new push is tried.
# `rm` it to retry the same commit (a flaky test).
FAILED="$DATA/.deploy-failed$SUFFIX"
# The suite's output. `pytest >/dev/null` left a rollback with nothing to read.
TESTLOG="$DATA/deploy-tests$SUFFIX.log"
TESTS_STARTED=0

# Every path that changed is documentation: nothing a bot imports, nothing the
# environment is built from. An allowlist on purpose -- anything not named here
# (a lock file, a unit, a script, a .txt) takes the full path. Paths git has to
# quote (non-ASCII) start with `"` and so fall through to the full path too.
docs_only() {  # old new
  local files f
  files=$(git diff --name-only "$1" "$2") || return 1
  while IFS= read -r f; do
    case "$f" in
      "") ;;
      docs/*|*.md) ;;
      *) return 1 ;;
    esac
  done <<EOF
$files
EOF
  return 0
}

check_sudoers_drift() {
  # Check the CAPABILITY, not the file. The first version of this compared
  # deploy/sudoers-ntx-deploy against /etc/sudoers.d/ntx-deploy with cmp -- but
  # that file is 0440 root:root and this runs as `ntx`, so the read failed on
  # permission every time and it reported drift forever, including right after a
  # correct install. A check that always fires is worse than no check (R70).
  #
  # `sudo -n -l` lists this account's own grants, needs no privilege, and answers
  # the only question that matters: can the deploy restart what it manages? It
  # reports and never installs -- anything able to write the repo's sudoers file
  # would otherwise be granting itself root on this box.
  local granted missing=""
  granted=$(sudo -n -l 2>/dev/null) || {
    log "could not list sudo grants; skipping the permission check"
    return 0
  }
  for u in $MANAGED_UNITS; do
    case "$granted" in
      *"systemctl stop $u"*) ;;
      *) missing="$missing $u" ;;
    esac
  done
  if [ -n "$missing" ]; then
    log "MISSING SUDO GRANT for:$missing"
    log "  these units are in MANAGED_UNITS but this deploy cannot restart them,"
    log "  so they keep running OLD CODE. A human must run, as root:"
    log "  install -m 440 $REPO/deploy/sudoers-ntx-deploy /etc/sudoers.d/ntx-deploy && visudo -c"
    notice "ntx deploy: no sudo grant to restart$missing -- they are running old code"
  fi
}

check_unit_drift() {
  local changed="" files=""
  for f in "$REPO"/deploy/*.service "$REPO"/deploy/*.timer; do
    local name; name=$(basename "$f")
    # ntx-s003.service points at $LIVE_REPO and asserts that path exists. Until
    # the live checkout has been created, telling an operator to install it is
    # telling them to keep the money node DOWN at its next restart. The
    # migration (scripts/setup_live_checkout.sh) installs it deliberately.
    if [ "$name" = "ntx-s003.service" ] && [ ! -d "$LIVE_REPO/.git" ]; then
      continue
    fi
    [ -f "/etc/systemd/system/$name" ] || { changed="$changed $name(new)"; files="$files $f"; continue; }
    cmp -s "$f" "/etc/systemd/system/$name" || { changed="$changed $name"; files="$files $f"; }
  done
  if [ -n "$changed" ]; then
    log "UNIT FILES CHANGED IN GIT but not installed:$changed"
    log "  a human must run, as root (it installs only these:$changed):"
    # Name the files. The old glob silently reinstalled every other unit too,
    # including ones the operator had not looked at.
    # Naming a script beats naming a command to retype: the hand-typed form is
    # how ntx-ui.service came to be installed in a state it could not start
    # from, and then sat `failed` for sixteen hours (F065 tail).
    log "  sudo $REPO/scripts/install_units.sh        # --dry-run to look first"
    notice "ntx deploy: systemd unit files changed in git and need installing by hand:$changed"
  fi
}

OLD=$(git rev-parse HEAD)
STOPPED=""

rollback() {  # [signal] -- interrupted, not failed: the commit may be fine
  trap - ERR INT TERM HUP   # a failure inside the rollback must not re-enter it
  log "FAILED -- rolling back to ${OLD:0:8}"
  if [ "$TESTS_STARTED" = 1 ] && [ -s "$TESTLOG" ]; then
    log "  last lines of the test run ($TESTLOG):"
    tail -n 15 "$TESTLOG" || true
  fi
  # A timeout or a stop of the update service is not evidence against the commit;
  # stamping it would pin the box to OLD until the next push.
  if [ "${1:-}" != signal ] && [ -n "${NEW:-}" ]; then
    printf '%s %s\n' "$NEW" "$(date +%s)" > "$FAILED" || true
  fi
  # Stop FIRST. On the unhealthy-start path the unit is already running, so the
  # old `start` was a no-op and the node kept running the bad code from memory
  # while the disk said otherwise (L2-06).
  for u in $STOPPED; do
    sudo -n systemctl stop "$u" || log "WARNING: could not stop $u"
  done
  git reset --hard --quiet "$OLD" || log "WARNING: git reset failed"
  sync_env || log "WARNING: rollback sync failed -- run 'make install' by hand"
  bad=""
  for u in $STOPPED; do
    sudo -n systemctl reset-failed "$u" 2>/dev/null
    touch "$STARTED"
    if sudo -n systemctl start "$u" && wait_healthy "$u"; then
      log "  $u is back on ${OLD:0:8}"
    else
      bad="$bad $u"
    fi
  done
  if [ -n "$bad" ]; then
    log "ROLLBACK INCOMPLETE -- DOWN:$bad"
    alert "ntx ROLLBACK INCOMPLETE:$bad is DOWN on the box"
  else
    alert "ntx deploy failed and rolled back to ${OLD:0:8}"
  fi
  exit 1
}
rollback_signal() { rollback signal; }

if [ "${NTX_DEPLOY_REEXEC:-}" = "1" ]; then
  # HEAD is already the new commit here, so the rollback target must come from
  # the environment BEFORE the trap is armed -- otherwise a failure after the
  # re-exec would "roll back" to the commit it was meant to undo.
  OLD=${NTX_DEPLOY_OLD:-$OLD}
  NEW=${NTX_DEPLOY_NEW:-$(git rev-parse HEAD)}
  STOPPED=${NTX_DEPLOY_STOPPED:-}
  log "running the updated deploy script (rollback target ${OLD:0:8})"
  trap rollback_signal INT TERM HUP
  trap rollback ERR
else
  # Never swap code underneath a deploy that is already running. One lock per
  # checkout: a live deploy and a soak deploy touch different working trees and
  # must not block each other (that is the whole point of the split), but two
  # runs against the SAME checkout still must not overlap.
  mkdir -p "$DATA"
  exec 9>"$DATA/update$SUFFIX.lock"
  if command -v flock >/dev/null && ! flock -n 9; then
    log "another update is running -- nothing to do"
    exit 0
  fi

  git fetch --quiet origin "$BRANCH"
  NEW=$(git rev-parse "origin/$BRANCH")
  if [ "$OLD" = "$NEW" ]; then
    if stamp_is_fresh "$OLD"; then
      # Nothing to do, and nothing was worth spending a core on.
      exit 0
    fi
    log "$ROLE checkout already at ${OLD:0:8} -- re-verifying the installed environment"
    sync_env
    "$UV" run --frozen pytest -q >/dev/null
    check_unit_drift
    write_stamp
    log "environment verified"
    exit 0
  fi

  log "updating the $ROLE checkout ($REPO): ${OLD:0:8} -> ${NEW:0:8}"
  if [ "$DRY" = 1 ]; then
    git --no-pager log --oneline "$OLD..$NEW" | head -20 || true  # head exits first; pipefail would abort the dry run
    log "(dry run: nothing stopped, pulled or restarted)"
    exit 0
  fi

  # A deliberate hold: `touch data/.deploy-hold` pins the box to the commit it is
  # on, so a hand rollback survives the next timer tick (D2-01).
  if [ -e "$DATA/.deploy-hold" ]; then
    log "deploy-hold is set ($DATA/.deploy-hold) -- staying on ${OLD:0:8}"
    exit 0
  fi

  # Demo only: the timer is what retries. A live sync is a human who has already
  # stopped the money node, and refusing then would waste that stop.
  if [ "$ROLE" = demo ] && [ -f "$FAILED" ] && [ "$(cut -d' ' -f1 "$FAILED")" = "$NEW" ]; then
    log "commit ${NEW:0:8} already failed here -- not retrying it (rm $FAILED to force)"
    exit 0
  fi

  # A docs-only push changes nothing a bot runs, so it stops nothing, syncs
  # nothing and runs no suite. Until 2026-10-07 every push -- a typo fix in a
  # runbook included -- stopped both demo bots for ~4 minutes and replayed their
  # adoption cycle. Only from a VERIFIED commit: the claim is "same code as one
  # that passed here", so it carries that commit's stamp time forward rather than
  # resetting the 6-hourly re-verify. Demo only: a live deploy always tests.
  if [ "$ROLE" = demo ] && stamp_is_fresh "$OLD" && docs_only "$OLD" "$NEW"; then
    log "docs-only change ${OLD:0:8} -> ${NEW:0:8}: fast-forwarding, nothing stopped or restarted"
    git merge --ff-only --quiet "origin/$BRANCH"
    read -r _ verified_at < "$STAMP"
    printf '%s %s\n' "$(git rev-parse HEAD)" "$verified_at" > "$STAMP"
    log "deployed $(git rev-parse --short HEAD) to the $ROLE checkout ($REPO) (docs only)"
    exit 0
  fi

  ACTIVE=$(units_active)

  if [ "$ROLE" = live ]; then
    # deploy-live.sh already waited for the stop to finish; this is the read-back
    # (R31). Nothing below may run with the live node up: it is installed
    # editable, so the merge would change the code it imports next.
    if ! live_unit_down; then
      log "REFUSING: ntx-s003.service is still up -- the admin must stop it first"
      exit 2
    fi
  else
    # Never restart through a signal (F054). The strategy decides on a closed 4h
    # bar and a restart replays history WITHOUT trading, so stopping the node in
    # the first minutes of a bar silently drops that bar's entry. The timer fires
    # every minute, so waiting costs almost nothing.
    # BOTH sides of the close (L2-09): a deploy that starts at 15:57 holds the
    # node down across 16:00 just as surely as one that starts at 16:01. The
    # "before" margin is the deploy's own length -- stop, sync, the suite (~4
    # min), start and the health wait -- with room to spare.
    # Live is not checked here: by this point the admin has ALREADY stopped that
    # node, so refusing would only throw away a stop that has been paid for.
    # `deploy-live.sh check` asks the same question before the stop (L3-06).
    window=0
    "$REPO/.venv/bin/python" -m ntx.ops.deploy_window \
      --quiet "${NTX_DEPLOY_QUIET_AFTER_BAR_S:-300}" \
      --before "${NTX_DEPLOY_BEFORE_BAR_S:-600}" || window=$?
    case "$window" in
      0) ;;
      1)
        log "commit ${NEW:0:8} waits for the bar window to clear"
        exit 0;;
      *)
        # Any other exit is the check itself broken (an ImportError, a crash),
        # and it used to read as "wait" -- every later deploy deferred forever
        # with one log line a minute and no push (L2-09). Refuse, loudly. Exit 0
        # on purpose: the alert is deduplicated, the OnFailure crash hook is not,
        # and this would fire it every minute.
        log "REFUSING: the bar-window check failed (exit $window) -- nothing deployed"
        alert "ntx deploy: the bar-window check is broken (exit $window); no demo deploy can run"
        exit 0;;
    esac
  fi

  # The journal is the only irreplaceable file on the box (R29). It is backed up
  # by the script that lives NEXT TO THE DATA: the live checkout's copy would
  # look in its own empty data/ and back up nothing, silently.
  [ -x "$DEMO_REPO/scripts/journal_backup.sh" ] && "$DEMO_REPO/scripts/journal_backup.sh" >/dev/null 2>&1 || true

  # Stop BEFORE pulling: the package is installed editable, so a running node
  # would start importing the new code at its next lazy import.
  mark_maintenance
  for u in $ACTIVE; do
    log "stopping $u"
    # A unit we have no grant for must not take the whole deploy down with it.
    # `set -e` plus `trap rollback ERR` turned a missing sudoers line into a
    # rolled-back deploy for every OTHER bot too -- so adding a unit to
    # MANAGED_UNITS before its grant was installed would have broken the
    # pipeline rather than just skipping that bot (F072). A guard may refuse an
    # action; it may not refuse to run (R73).
    if ! sudo -n systemctl stop "$u"; then
      log "WARNING: no permission to stop $u -- skipping it this deploy."
      log "  it is running OLD CODE until this is fixed, as root:"
      log "  install -m 440 $REPO/deploy/sudoers-ntx-deploy /etc/sudoers.d/ntx-deploy"
      notice "ntx deploy: cannot restart $u (missing sudoers grant); it is running old code"
      continue
    fi
    STOPPED="$STOPPED $u"
  done

  # Re-checked here, after the stop: the live node may have been started again
  # between the check above and this point (L2-02's sampling gap). A demo deploy
  # cannot reach the live checkout at all any more, so this is the live role only.
  if [ "$ROLE" = live ] && ! live_unit_down; then
    log "ntx-s003.service came back up while preparing -- not applying ${NEW:0:8}"
    exit 2
  fi

  trap rollback_signal INT TERM HUP
  trap rollback ERR
  git merge --ff-only "origin/$BRANCH"
  sync_env

  if ! git diff --quiet "$OLD" HEAD -- deploy/update.sh; then
    bash -n deploy/update.sh
    export NTX_DEPLOY_REEXEC=1 NTX_DEPLOY_OLD="$OLD" NTX_DEPLOY_NEW="$NEW" NTX_DEPLOY_STOPPED="$STOPPED"
    exec "$REPO/deploy/update.sh"
  fi
fi

# The gate that earns its place here: does this code work ON THIS BOX. ruff and
# the shell syntax check already ran on GitHub for this exact commit.
log "running the test suite (output: $TESTLOG)"
TESTS_STARTED=1
"$UV" run --frozen pytest -q > "$TESTLOG" 2>&1
log "tests pass"

for u in $STOPPED; do
  log "starting $u"
  # A unit that has hit StartLimitBurst refuses every later start until the
  # failure is cleared, and a deploy is a stop+start -- so a run of deploys
  # inside StartLimitIntervalSec locks the bot out of its own restart. That
  # happened twice on 2026-09-24 and left the soak down until a human cleared it.
  # reset-failed starts nothing by itself, and a box without the grant must not
  # take the whole deploy down over it.
  sudo -n systemctl reset-failed "$u" 2>/dev/null || true
  touch "$STARTED"
  sudo -n systemctl start "$u"
  if ! wait_healthy "$u"; then
    log "$u did not become healthy within 180 s"
    rollback
  fi
  log "  $u is up and healthy"
done

# The dashboard is a long-lived python process: new code on disk is not new code
# in memory. It kept serving the previous commit's UI after every deploy, which
# is how a picker fix shipped at 09:47 was still missing at 10:05. It is
# read-only, loopback-only and holds no position, so restarting it is free --
# and unlike the bots, nothing about it is worth a rollback.
if systemctl list-unit-files --no-legend -- ntx-ui.service >/dev/null 2>&1; then
  if sudo -n systemctl restart ntx-ui.service 2>/dev/null; then
    log "restarted ntx-ui.service onto the new code"
  else
    log "WARNING: could not restart ntx-ui.service -- it is serving older code"
  fi
fi

check_unit_drift
check_sudoers_drift
write_stamp
rm -f "$FAILED"
trap - ERR INT TERM HUP
log "deployed $(git rev-parse --short HEAD) to the $ROLE checkout ($REPO)"
