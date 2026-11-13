#!/usr/bin/env bash
# ONE-TIME migration: give the LIVE node its own checkout.
#
#   sudo -u ntx /home/ntx/ntx/scripts/setup_live_checkout.sh
#
# Before: both bots ran from /home/ntx/ntx, so `deploy/update.sh` had to refuse
# every commit while the live unit was up -- which froze continuous deployment
# for the demo soak as well (443 refused timer ticks on 2026-09-24/25).
#
# After:
#   /home/ntx/ntx        DEMO checkout: the soak, fast-forwarded every minute
#   /home/ntx/ntx-live   LIVE checkout: code only, moved only by `make deploy-live`
#
# The live checkout owns CODE ONLY. .env, data/, logs/ and reports/ are symlinks
# back to the demo checkout, so there is still exactly one journal, one set of
# state snapshots, one lock directory and one LIVE_ARMED file -- the monitor and
# the alerter keep reading one place, and nothing is moved or copied (R29).
#
# Idempotent: safe to re-run. It NEVER moves the live checkout's HEAD -- only
# `deploy/deploy-live.sh` may do that -- and it never deletes anything.
set -euo pipefail

DEMO_REPO=${NTX_DEMO_REPO:-/home/ntx/ntx}
LIVE_REPO=${NTX_LIVE_REPO:-/home/ntx/ntx-live}
LIVE_UNIT=ntx-s003.service
BOT_USER=${NTX_USER:-ntx}
# 1 = do the full read-back (import from the new venv + the whole test suite).
# The repo's own tests set 0 because they stub `uv`; a skipped verification says
# so in capitals rather than passing quietly (R70).
VERIFY=${NTX_SETUP_VERIFY:-1}

log()  { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die()  { log "REFUSING: $*"; exit 2; }

# --- refusals ------------------------------------------------------------- #
[ "$(id -un)" = "$BOT_USER" ] || die "run this as $BOT_USER:  sudo -u $BOT_USER $0"

# The live node must be DOWN. Its checkout is about to be created under the path
# its unit will point at; doing that while it trades is how you get a node whose
# code on disk is not the code it is running. `is-active` alone is not enough: a
# unit sits in `activating (auto-restart)` for the whole RestartSec window (L2-02).
case "$(systemctl show -p ActiveState --value "$LIVE_UNIT" 2>/dev/null)" in
  inactive|failed|"") ;;
  *) die "$LIVE_UNIT is up. Stop it first, with the account flat:
    make deploy-live        # or: sudo systemctl stop $LIVE_UNIT" ;;
esac

[ -d "$DEMO_REPO/.git" ]  || die "no git repository at $DEMO_REPO"
[ -f "$DEMO_REPO/.env" ]  || die "no $DEMO_REPO/.env -- the live node reads it through a symlink"
[ "$DEMO_REPO" != "$LIVE_REPO" ] || die "the two checkouts must be different directories"
case "$LIVE_REPO/" in "$DEMO_REPO"/*) die "$LIVE_REPO is inside $DEMO_REPO -- git would treat it as untracked files";; esac
if [ -e "$LIVE_REPO" ] && [ ! -d "$LIVE_REPO/.git" ]; then
  die "$LIVE_REPO exists but is not a git repository -- move it aside by hand"
fi

ORIGIN=$(git -C "$DEMO_REPO" remote get-url origin 2>/dev/null || true)
[ -n "$ORIGIN" ] || die "$DEMO_REPO has no 'origin' remote: the live checkout would have nothing to deploy from"

UV=$(command -v uv || true)
[ -x "$UV" ] || UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || UV=/home/ntx/.local/bin/uv
[ -x "$UV" ] || die "uv not found (PATH, \$HOME/.local/bin, /home/ntx/.local/bin)"

# --- the checkout --------------------------------------------------------- #
if [ -d "$LIVE_REPO/.git" ]; then
  log "live checkout already exists at $LIVE_REPO ($(git -C "$LIVE_REPO" rev-parse --short HEAD))"
  log "  leaving its HEAD exactly where it is -- only 'make deploy-live' moves it"
else
  log "creating the live checkout at $LIVE_REPO from $DEMO_REPO"
  # Local clone: no network, and it starts at the commit the soak is already
  # running and testing. --no-hardlinks so a future `git gc` in one checkout can
  # never prune an object the other still needs.
  git clone --quiet --no-hardlinks "$DEMO_REPO" "$LIVE_REPO"
  git -C "$LIVE_REPO" remote set-url origin "$ORIGIN"
  log "  cloned at $(git -C "$LIVE_REPO" rev-parse --short HEAD), origin -> $ORIGIN"
fi

# `git fetch` proves the live checkout can reach GitHub as this user -- if the
# deploy key is not readable here, better to learn it now than mid-deploy.
if git -C "$LIVE_REPO" fetch --quiet origin main 2>/dev/null; then
  log "  origin reachable: origin/main is $(git -C "$LIVE_REPO" rev-parse --short origin/main)"
else
  log "  WARNING: could not fetch origin/main from $LIVE_REPO -- deploy-live will fail until that works"
fi

# --- one data directory, one .env ----------------------------------------- #
# Symlinks, not copies: two journals or two .env files is the failure this whole
# layout exists to avoid.
mkdir -p "$DEMO_REPO/logs" "$DEMO_REPO/reports" "$DEMO_REPO/data"
for name in .env data logs reports; do
  ln -sfn "$DEMO_REPO/$name" "$LIVE_REPO/$name"
  log "  $LIVE_REPO/$name -> $DEMO_REPO/$name"
done

# .gitignore has /data/, /logs/ and /reports/ with a trailing slash, which match
# directories only -- a symlink of the same name shows up as an untracked file in
# every `git status` of the live checkout. This is exactly what info/exclude is for.
EXCLUDE="$LIVE_REPO/.git/info/exclude"
mkdir -p "$(dirname "$EXCLUDE")"
for name in data logs reports; do
  grep -qxF "/$name" "$EXCLUDE" 2>/dev/null || printf '/%s\n' "$name" >> "$EXCLUDE"
done

# --- the venv ------------------------------------------------------------- #
# Same flags as `make install` and as deploy/update.sh, or the live node runs a
# different environment from the one the tests were green in.
log "installing the live venv (uv sync --frozen --python 3.12 --all-extras)"
(cd "$LIVE_REPO" && "$UV" sync --frozen --python 3.12 --all-extras --quiet)

# --- read back what was built (R31) --------------------------------------- #
if [ "$VERIFY" != 1 ]; then
  log "VERIFICATION SKIPPED (NTX_SETUP_VERIFY=$VERIFY): this run proves nothing"
  log "  about the venv or the tests. Re-run without it before trusting the node."
else
  PY="$LIVE_REPO/.venv/bin/python"
  [ -x "$PY" ] || die "uv sync produced no interpreter at $PY"
  ROOT=$("$PY" -c 'from ntx.config.settings import REPO_ROOT; print(REPO_ROOT)')
  LIVE_PHYS=$(cd "$LIVE_REPO" && pwd -P)
  [ "$ROOT" = "$LIVE_PHYS" ] || die "the live venv imports ntx from $ROOT, not $LIVE_PHYS"
  log "  ntx imports from $ROOT"
  log "running the test suite in the live checkout"
  (cd "$LIVE_REPO" && "$UV" run --frozen pytest -q >/dev/null)
  log "  tests pass"
fi

cat <<NEXT

$LIVE_REPO is ready ($(git -C "$LIVE_REPO" rev-parse --short HEAD)).

Nothing is using it yet: the installed $LIVE_UNIT still points at $DEMO_REPO.
Finish as ROOT, in this order:

  sudo install -m 644 $DEMO_REPO/deploy/*.service $DEMO_REPO/deploy/*.timer /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl start $LIVE_UNIT
  sudo -u ntx $DEMO_REPO/deploy/deploy-live.sh verify

Never install the unit file before this script has run: the new unit points at
$LIVE_REPO, and systemd would fail to start a node whose checkout does not exist.
NEXT
