#!/usr/bin/env bash
# Install the systemd unit files that differ from what is in git.
#
# `update.sh` can detect unit drift but must never fix it: installing a unit is a
# root action, and the deploy runs as the bot user. So it printed a command and
# hoped. On 2026-09-26 that gap cost sixteen hours -- ntx-ui.service was
# installed by hand in a form that could not start, the drift check then kept
# reporting a *different* drift, and nobody noticed the unit was `failed`
# because a stray process was still serving the page.
#
#     sudo /home/ntx/ntx/scripts/install_units.sh            # show and install
#     sudo /home/ntx/ntx/scripts/install_units.sh --dry-run  # show only
#
# What it will NOT do:
#   * restart anything that trades. Installing a unit file changes what the NEXT
#     start uses; it does not touch a running node, and the LIVE node is never
#     restarted by automation (R57). `deploy-live.sh` is the only path to that.
#   * install ntx-s003.service before the live checkout exists -- that unit
#     asserts a path that is not there yet, so installing it early arms a
#     failure at the money node's next start.
#   * touch a unit whose content already matches git.
#   * install anything that could run as root (L3-03). The checkout it reads is
#     owned by the bot user and fast-forwarded from GitHub, and this runs under
#     sudo -- so a pushed `User=root` would be the step from "ntx" to "root".
#     Every changed unit must be named ntx-*, run as User=ntx, and execute only
#     from /home/ntx/ without a privileged (+ or !) prefix; anything else
#     refuses the WHOLE run. What would change is printed as a diff and must be
#     confirmed by typing `install` (or passing --yes after reading --dry-run).
set -euo pipefail

REPO=${NTX_REPO:-/home/ntx/ntx}
LIVE_REPO=${NTX_LIVE_REPO:-/home/ntx/ntx-live}
DEST=${NTX_UNIT_DEST:-/etc/systemd/system}
DRY=0
YES=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --yes) YES=1 ;;
    *) echo "usage: $0 [--dry-run] [--yes]" >&2; exit 2 ;;
  esac
done

log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

if [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then
  echo "must run as root: sudo $0" >&2
  exit 2
fi
[ -d "$REPO/deploy" ] || { echo "no $REPO/deploy" >&2; exit 2; }

changed=()
for f in "$REPO"/deploy/*.service "$REPO"/deploy/*.timer; do
  [ -e "$f" ] || continue
  name=$(basename "$f")
  # The money node's unit points at $LIVE_REPO and asserts that path exists.
  # Installing it before the migration has run means its next start fails.
  if [ "$name" = "ntx-s003.service" ] && [ ! -d "$LIVE_REPO/.git" ]; then
    log "skip $name -- the live checkout does not exist yet"
    continue
  fi
  if [ ! -f "$DEST/$name" ]; then
    changed+=("$f"); log "NEW      $name"
  elif ! cmp -s "$f" "$DEST/$name"; then
    changed+=("$f"); log "CHANGED  $name"
  fi
done

if [ ${#changed[@]} -eq 0 ]; then
  log "every unit file matches git -- nothing to install"
  exit 0
fi

# Review every changed file before anything is shown as installable. Prints one
# line per problem; any problem refuses the whole run.
review() {
  local f=$1 name users line cmd prefix
  name=$(basename "$f")
  case "$name" in
    ntx-*.service|ntx-*.timer) ;;
    *) echo "$name: not named ntx-* -- it could replace a system unit" ;;
  esac
  if [[ "$name" == *.timer ]]; then
    if grep -E '^[[:space:]]*Unit=' "$f" | grep -vqE '^[[:space:]]*Unit=ntx-[A-Za-z0-9@_.-]+$'; then
      echo "$name: Unit= names a unit that is not ntx-*"
    fi
    return 0
  fi
  users=$(grep -E '^[[:space:]]*User=' "$f" | sed -E 's/^[[:space:]]*User=//; s/[[:space:]]*$//' || true)
  [ "$users" = "ntx" ] || echo "$name: User= must be exactly 'ntx' (got: ${users:-none, i.e. root})"
  if grep -E '^[[:space:]]*Group=' "$f" | grep -vqE '^[[:space:]]*Group=ntx[[:space:]]*$'; then
    echo "$name: Group= is not ntx"
  fi
  grep -E '^[[:space:]]*(AmbientCapabilities|PermissionsStartOnly|SupplementaryGroups|CapabilityBoundingSet|DynamicUser)=' "$f" \
    | sed "s/^/$name: privileged setting: /" || true
  # After systemd's harmless prefixes (- @ :) every command must start in
  # /home/ntx/; `+` and `!` run it with full privileges whatever User= says.
  while IFS= read -r line; do
    cmd=${line#*=}
    prefix=${cmd%%/*}
    if [[ "$prefix" == *[+!]* ]]; then
      echo "$name: privileged Exec prefix: $line"
    elif [[ "${cmd#"$prefix"}" != /home/ntx/* ]] || [[ "$prefix" =~ [^-@:] ]]; then
      echo "$name: Exec outside /home/ntx/: $line"
    fi
  done < <(grep -E '^[[:space:]]*Exec[A-Za-z]*=' "$f" || true)
}

problems=""
for f in "${changed[@]}"; do
  problems+="$(review "$f")"$'\n'
done
problems=$(printf '%s' "$problems" | sed '/^$/d')
if [ -n "$problems" ]; then
  printf '%s\n' "$problems" >&2
  echo "REFUSING to install anything: a unit above could run as root or replace a" >&2
  echo "system unit. Read its history in git before going further (L3-03)." >&2
  exit 3
fi

for f in "${changed[@]}"; do
  name=$(basename "$f")
  echo "----- $name"
  if [ -f "$DEST/$name" ]; then
    diff -u "$DEST/$name" "$f" || true
  else
    sed 's/^/+/' "$f"
  fi
done

if [ "$DRY" = 1 ]; then
  log "--dry-run: ${#changed[@]} unit(s) would be installed"
  exit 0
fi

if [ "$YES" != 1 ]; then
  read -r -p "Install the ${#changed[@]} unit(s) above as root? Type install: " ans || ans=""
  [ "$ans" = "install" ] || { echo "not confirmed -- nothing installed" >&2; exit 2; }
fi

install -m 644 "${changed[@]}" "$DEST/"
systemctl daemon-reload
log "installed ${#changed[@]} unit(s) and reloaded systemd"

# A unit that is `failed` stays failed across a reinstall: systemd remembers the
# start-limit counter, so the new file never gets a chance. Clearing it is the
# whole point of reinstalling a unit that could not start.
for f in "${changed[@]}"; do
  name=$(basename "$f")
  if [ "$(systemctl is-failed "$name" 2>/dev/null)" = "failed" ]; then
    systemctl reset-failed "$name"
    log "cleared the failed state of $name"
  fi
done

log "done. Nothing was restarted: a new unit file applies at the next start."
log "  a stopped, non-trading unit can be started now, e.g.:"
log "    systemctl enable --now ntx-ui.service"
