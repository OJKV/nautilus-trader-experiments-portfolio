#!/usr/bin/env bash
# Deploy new code to the LIVE node. The one deploy nothing does automatically.
#
#   sudo -u ntx /home/ntx/ntx/deploy/deploy-live.sh <status|check|sync|verify>
#
# TWO CHECKOUTS (docs/24 §1b, since 2026-09-25):
#
#   /home/ntx/ntx        DEMO checkout -- the soak; ntx-update.timer fast-forwards
#                        it every minute. THIS SCRIPT LIVES HERE, so the driver is
#                        always the newest reviewed one, and the sudoers grant
#                        (administrator -> /home/ntx/ntx/deploy/deploy-live.sh) is unchanged.
#   /home/ntx/ntx-live   LIVE checkout -- the money node. Nothing moves it except
#                        this script. THIS SCRIPT OPERATES HERE.
#
# Both share ONE data directory (the demo checkout's data/), because journals,
# state snapshots, locks and the arming file are already namespaced per bot and
# the monitor reads them from one place (R29, R56).
#
# The timer deploys the demo soak by itself and can no longer be blocked by the
# live node -- that shared-checkout freeze is exactly what this split removes.
# A live deploy is still a deliberate act: restarting a live node is an adoption
# cycle, the venue stop is released and re-placed (F044), and a failure there can
# flatten a real position (R14). So:
#
#   1. the LIVE account must be FLAT at the venue (not the journal, the venue)
#   2. a human types DEPLOY LIVE
#   3. stop -> pull -> test in the LIVE checkout -> start   (deploy/update.sh)
#   4. the node must come back writing a HEALTHY live snapshot, or the deploy
#      rolls itself back
#
# With an open position it stops before touching anything: wait for the position
# to close, or close it deliberately with `python -m ntx.ops.flatten --live`.
set -euo pipefail

# The two paths are a contract shared with deploy/update.sh,
# scripts/setup_live_checkout.sh and deploy/ntx-s003.service.
DEMO_REPO=${NTX_DEMO_REPO:-/home/ntx/ntx}
LIVE_REPO=${NTX_LIVE_REPO:-/home/ntx/ntx-live}
CONFIRM="DEPLOY LIVE"
# A DIFFERENT sentence for the harder case. Not a formality: the whole point is
# that it cannot be answered from muscle memory by someone who has typed the
# ordinary one twenty times.
CONFIRM_OPEN="DEPLOY OVER OPEN POSITIONS"
LIVE_UNIT=ntx-s003.service
# Every python call below must read the SHARED data directory, not the live
# checkout's (empty) one: that is where the journal, the state snapshot and
# LIVE_ARMED live. The unit sets the same variable.
export NTX_DATA_DIR=${NTX_DATA_DIR:-$DEMO_REPO/data}
DATA=$NTX_DATA_DIR
# The live checkout's own venv: the deploy must exercise the interpreter that
# will actually run the money node, not the soak's.
PY="$LIVE_REPO/.venv/bin/python"

log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

PHASE=${1:-}
YES=${2:-}
case "$PHASE" in
  status|check|sync|verify) ;;
  *)
    cat >&2 <<USAGE
This script runs in three phases, because stopping and starting the LIVE unit is
the ADMIN account's privilege, not the bot user's (L2-01) -- and this script runs
as the bot user. \`make deploy-live\` orchestrates them:

  sudo -u ntx deploy/deploy-live.sh check    # account flat? confirm?
  sudo systemctl stop $LIVE_UNIT       # ADMIN
  sudo -u ntx deploy/deploy-live.sh sync     # pull, test in the live checkout
  sudo systemctl start $LIVE_UNIT      # ADMIN
  sudo -u ntx deploy/deploy-live.sh verify   # healthy snapshot, or it failed

  sudo -u ntx deploy/deploy-live.sh status   # read-only: where each checkout is

Run: make deploy-live
USAGE
    exit 2;;
esac

live_head() { git -C "$LIVE_REPO" rev-parse --short HEAD 2>/dev/null || echo "unknown"; }

if [ "$PHASE" = status ]; then
  # Read-only. The question the split creates -- "are the two bots on the same
  # code?" -- must have an answer that needs no privileges and changes nothing.
  if [ -d "$LIVE_REPO/.git" ]; then
    printf 'live checkout  %s  %s\n' "$LIVE_REPO" "$(live_head)"
  else
    printf 'live checkout  %s  MISSING -- run scripts/setup_live_checkout.sh\n' "$LIVE_REPO"
  fi
  printf 'demo checkout  %s  %s\n' "$DEMO_REPO" \
    "$(git -C "$DEMO_REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  if [ -d "$LIVE_REPO/.git" ]; then
    behind=$(git -C "$DEMO_REPO" rev-list --count "$(live_head)"..HEAD 2>/dev/null || echo "?")
    printf 'the live node is %s commit(s) behind the demo checkout\n' "$behind"
  fi
  printf 'data (shared)  %s\n' "$DATA"
  exit 0
fi

if [ ! -d "$LIVE_REPO/.git" ]; then
  log "REFUSING: there is no live checkout at $LIVE_REPO."
  log "  One-time migration, as the bot user:  $DEMO_REPO/scripts/setup_live_checkout.sh"
  log "  (docs/25 'Migrating to the two-checkout layout')"
  exit 2
fi
if [ ! -x "$PY" ]; then
  log "REFUSING: no venv at $LIVE_REPO/.venv -- re-run scripts/setup_live_checkout.sh"
  exit 2
fi

if [ "$PHASE" = check ]; then
  log "1/3 reading the LIVE account"
  # Three outcomes, and they must stay three (R70). 0 flat, 1 holding
  # something, anything else means the account could not be read -- which is
  # never a door, only a refusal.
  set +e
  "$PY" -m ntx.ops.venue_state --live
  state=$?
  set -e
  case "$state" in
    0)
      NEED="$CONFIRM"
      ;;
    1)
      # S003 holds a position most of the time -- measured flat 31.5% of it,
      # worst-case holding stretch 28.5 days. Refusing here is not a wait, it
      # is a deadlock, and a gate nobody can pass gets worked around. So say
      # exactly what the restart will cost and ask a harder question.
      log "the LIVE account is NOT flat. What a restart would cost:"
      "$PY" -m ntx.ops.restart_cost --live || {
        log "REFUSING: could not describe the cost of restarting over open positions."
        exit 2
      }
      log "Nothing else about the positions changes: same size, same stop price."
      log "To stay flat-only instead: wait, or $PY -m ntx.ops.flatten --live"
      NEED="$CONFIRM_OPEN"
      ;;
    *)
      log "REFUSING: the live account could not be read (exit $state)."
      log "  Not knowing whether a position is open is not the same as there being none."
      exit 2
      ;;
  esac
  # `--yes` is how an operator with standing authorisation runs this from a
  # script. It cannot be an environment variable: sudo strips those, so there was
  # no supported automated path at all.
  #
  # `--yes` covers the ordinary door only. Deploying over an open position is a
  # deliberate act every time, so it is always typed -- by hand, or by an agent
  # the owner has just told to do it in this specific case.
  if [ "$NEED" = "$CONFIRM_OPEN" ] || { [ "$YES" != "--yes" ] && [ "${NTX_DEPLOY_LIVE_YES:-}" != "$NEED" ]; }; then
    if [ "${NTX_DEPLOY_LIVE_YES:-}" = "$NEED" ]; then
      log "confirmed by NTX_DEPLOY_LIVE_YES"
    else
      read -r -p "LIVE node, real money. Type $NEED to continue: " typed
      if [ "$typed" != "$NEED" ]; then
        log "not confirmed -- nothing changed"
        exit 2
      fi
    fi
  fi
  # Never restart through a signal (R65, F054; L3-06). A restart replays history
  # WITHOUT trading, so a node down across a 4h close (00/04/08/12/16/20 UTC), or
  # restarted in the minutes after one, silently drops that bar's entry or exit.
  # Asked AFTER the confirmation, so time spent at the prompt cannot carry the
  # stop into the window. "Before" covers the whole three-phase deploy: stop,
  # sync + suite in the live checkout (~5 min), start, adoption.
  # The DEMO checkout's interpreter on purpose: it holds the module that matches
  # this script, while the live checkout is the old code being replaced and may
  # not know `--before`. The question is clock arithmetic, not money.
  window=0
  "$DEMO_REPO/.venv/bin/python" -m ntx.ops.deploy_window \
    --quiet "${NTX_DEPLOY_QUIET_AFTER_BAR_S:-300}" \
    --before "${NTX_DEPLOY_LIVE_BEFORE_BAR_S:-900}" || window=$?
  case "$window" in
    0) ;;
    1)
      log "REFUSING: too close to a 4h bar close -- a restart now would miss that bar (R65)."
      log "  Nothing changed. Run it again once the window above has passed."
      exit 2;;
    *)
      log "REFUSING: the bar-window check itself failed (exit $window) -- not guessing the clock."
      exit 2;;
  esac
  # The admin stops the node next and it stays down through sync + suite (~7 min).
  # Tell the alerter it is a deploy, and whose: without this a live deploy pushed
  # CRITICAL "node down or frozen?" (2026-10-11). update.sh's EXIT trap in the
  # sync phase removes it; it also expires on its own after 15 min.
  printf '%s live\n' "$(date +%s)" > "$DATA/.deploy-in-progress" || true
  log "checks passed -- the admin may stop the unit now"
  exit 0
fi

if [ "$PHASE" = sync ]; then
  # `systemctl stop` returns before the node has finished shutting down -- it
  # takes about ten seconds to close its venue clients -- and update.sh refuses
  # to touch the live checkout while the unit is up. On 2026-09-24 that raced:
  # the sync was refused one second after the stop, and the whole deploy applied
  # nothing while reporting success.
  for _ in $(seq 1 30); do
    systemctl is-active --quiet "$LIVE_UNIT" || break
    sleep 2
  done
  if systemctl is-active --quiet "$LIVE_UNIT"; then
    log "REFUSING: the live unit is still active after 60s -- not touching the checkout"
    exit 2
  fi
  log "2/3 pull, test and install IN THE LIVE CHECKOUT ($LIVE_REPO)"
  # NTX_DEPLOY_LIVE_CHECKOUT=1 is the only thing that lets update.sh advance the
  # live checkout, and it is set here and nowhere else. The script being run is
  # the live checkout's own copy: `update.sh` acts on the tree it sits in.
  # It stops and starts nothing -- the admin owns the live unit (L2-01).
  NTX_DEPLOY_LIVE_CHECKOUT=1 "$LIVE_REPO/deploy/update.sh"
  exit $?
fi

log "3/3 confirming the deploy happened AND the node is healthy"

# FIRST: is the running unit even using this checkout? Nothing else here can
# tell. `git -C $LIVE_REPO rev-parse HEAD` passes as soon as the live checkout
# was advanced, and the snapshot is written into the SHARED data directory
# whichever checkout the node runs from -- so if the unit file was never
# installed, both checks pass while the money node keeps running the DEMO
# checkout, the one the timer fast-forwards every minute. That is precisely the
# situation the split exists to prevent, reported as success.
INSTALLED_WD=$(systemctl show ntx-s003.service -p WorkingDirectory --value 2>/dev/null)
INSTALLED_EXEC=$(systemctl show ntx-s003.service -p ExecStart --value 2>/dev/null)
case "$INSTALLED_WD$INSTALLED_EXEC" in
  *"$LIVE_REPO"*) ;;
  *)
    log "THE LIVE UNIT IS NOT USING THIS CHECKOUT."
    log "  expected: $LIVE_REPO"
    log "  installed WorkingDirectory: ${INSTALLED_WD:-<unset>}"
    log "  the money node is running code from somewhere else -- install the unit:"
    log "  sudo sh -c 'install -m 644 $LIVE_REPO/deploy/ntx-s003.service /etc/systemd/system/' && sudo systemctl daemon-reload"
    exit 1;;
esac

# SECOND: was this commit actually tested HERE? write_stamp runs only after the
# suite passes in this checkout, so the stamp is the evidence. Without it, a sync
# interrupted by a signal leaves the checkout on an untested commit and the unit
# stopped-but-enabled, so the next start runs money on code whose tests never
# finished.
STAMP="$DATA/.deploy-verified-live"
# The stamp is "<sha> <unix-ts>" (update.sh write_stamp), so comparing the whole
# line to a bare sha never matched -- and the failure message prints only the
# first 8 characters of each, so it read "stamp 9f59ea12, HEAD 9f59ea12" and
# called that a mismatch. The last gate before real money failed every time a
# stamp existed (2026-09-29). Compare the sha.
STAMPED=$(cut -d' ' -f1 "$STAMP" 2>/dev/null || echo "")

# A healthy snapshot only proves the OLD code is running well. Check the code
# actually moved: if update.sh refused, the checkout is still behind origin/main.
git -C "$LIVE_REPO" fetch -q origin 2>/dev/null || true
HEAD_SHA=$(git -C "$LIVE_REPO" rev-parse HEAD)
WANT_SHA=$(git -C "$LIVE_REPO" rev-parse origin/main 2>/dev/null || echo "$HEAD_SHA")
if [ "$HEAD_SHA" != "$WANT_SHA" ]; then
  log "THE DEPLOY DID NOT HAPPEN: $LIVE_REPO is at ${HEAD_SHA:0:8}, origin/main is ${WANT_SHA:0:8}"
  log "  the node was restarted but is running the OLD code -- read the sync output above"
  exit 1
fi
if [ -n "$STAMPED" ] && [ "$STAMPED" != "$HEAD_SHA" ]; then
  log "THIS COMMIT WAS NOT TESTED IN THIS CHECKOUT: stamp ${STAMPED:0:8}, HEAD ${HEAD_SHA:0:8}"
  log "  the sync did not finish -- the node is running untested code. Re-run the deploy."
  exit 1
fi
# The node needs ~30-60 s to connect, warm up and write its first snapshot, and
# this phase runs immediately after `systemctl start`. Poll, or a healthy deploy
# reports a failure that sends an operator looking for a problem that is not
# there -- and a stale snapshot from before the stop is not proof either, which
# is why --max-age stays tight instead of being relaxed.
SNAP="$DATA/state/S003-live.json"
DEADLINE=$((SECONDS + 180))
while [ $SECONDS -lt $DEADLINE ]; do
  "$PY" -m ntx.ops.snapshot_check "$SNAP" --live --max-age 120 >/dev/null 2>&1 && break
  sleep 5
done
if ! "$PY" -m ntx.ops.snapshot_check "$SNAP" --live --max-age 120; then
  log "THE LIVE NODE IS NOT HEALTHY after the deploy -- check it now:"
  log "  journalctl -u $LIVE_UNIT -n 50"
  log "  $PY -m ntx.ops.venue_state --live"
  exit 1
fi
log "live deploy complete: $(live_head) in $LIVE_REPO"
