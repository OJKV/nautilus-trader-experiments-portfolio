#!/usr/bin/env bash
# R44 drill: restart a node while it holds a real, protected position.
#
# The adoption path -- release the venue stop, re-place it, adopt the position --
# was rewritten after L-09/L-10 and again after L2-04/L2-05, and has never been
# exercised against a real position. It is the code that runs on EVERY restart of
# a node that holds money, and its old failure mode flattened a healthy position.
# Unit tests cannot prove it: only the venue can (R44, R11).
#
# DEMO ONLY. It refuses to run against the live account, and it refuses to run
# while the soak is up -- two nodes on one sub-account fight over one position and
# the venue cannot tell them apart (F046/R51), and the soak's own watchdog would
# now close the drill's position as an orphan (N2-04).
#
#   sudo -u ntx /home/ntx/ntx/scripts/drill_restart.sh
#
# Leaves the account flat and the soak running, and writes a report under
# reports/rehearsals/.
set -uo pipefail
REPO=${REPO:-/home/ntx/ntx}
PY="$REPO/.venv/bin/python"
cd "$REPO" || { echo "no repo at $REPO"; exit 2; }

say() { printf '%s  %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { printf '\nDRILL ABORTED: %s\n' "$*" >&2; exit 1; }

# ---- refuse anything that is not a demo drill ------------------------------- #
grep -qE '^NTX_VENUE=okx_demo' "$REPO/.env" || die ".env does not select okx_demo"
[ -e /etc/ntx/LIVE_ARMED ] && die "live is ARMED -- disarm before drilling"
# The LIVE unit may keep running: it trades a DIFFERENT OKX sub-account, with its
# own journal, state file and pid lock. This drill touches only the demo account
# (NTX_VENUE=okx_demo above), stops only ntx-soak.service, flattens only --demo,
# and only ever kills a process matching "--strategy S900" -- which the live node,
# running S003, can never match. Refusing outright would mean never drilling again
# now that live is up, and a safety mechanism that can no longer be rehearsed
# decays into an assumption.
if systemctl is-active --quiet ntx-s003.service; then
  say "note: the LIVE unit is running on its own sub-account and is not touched by this drill"
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$REPO/reports/rehearsals/$STAMP-r44-restart-drill"
mkdir -p "$OUT"
exec > >(tee "$OUT/drill.log") 2>&1
say "R44 restart drill, demo account, $STAMP"

SOAK_WAS_UP=0
if systemctl is-active --quiet ntx-soak.service; then
  SOAK_WAS_UP=1
  say "stopping the soak (one node per account, F046)"
  sudo -n systemctl stop ntx-soak.service || die "could not stop ntx-soak"
fi
NODE=""
restore() {
  # Every abort path goes through this trap, and a node left running holds the
  # pid lock (R51) -- which is exactly what refused the next drill run: "NTX-900
  # (demo) is already running as pid 260506". The lock was right; the drill was
  # the one leaking processes.
  if [ -n "${NODE:-}" ] && kill -0 "$NODE" 2>/dev/null; then
    kill -9 "$NODE" 2>/dev/null
    wait "$NODE" 2>/dev/null
  fi
  pkill -9 -f "ntx.live.node --strategy S900" 2>/dev/null
  # A drill's leftovers must not outlive it: a stale S900 snapshot keeps
  # reporting killed/unhealthy in vps_check and the monitor long after the
  # drill is over, which is exactly the noise that teaches an operator to
  # ignore a red check (R70).
  rm -f "$REPO/data/state/S900-demo.json" "$REPO/data/state/S900-demo.json.killed" \
        "$REPO/data/locks/NTX-900-demo.lock"
  # FLATTEN BEFORE THE SOAK COMES BACK. The drill and the soak share one OKX demo
  # sub-account, so any position this drill leaves behind is a position the soak
  # will find at the venue, adopt as its own and journal under S003 (F046/R51).
  # That happened: two SOL trades the Donchian strategy never chose are now in
  # data/journal-demo-S003.db, marked stop_refused and closed_while_offline.
  # An abort path that skips this quietly corrupts the soak's own record.
  echo "FLATTEN DEMO" | "$PY" -m ntx.ops.flatten --demo >> "$OUT/cleanup.txt" 2>&1

  if [ "$SOAK_WAS_UP" = 1 ]; then
    say "restarting the soak"
    sudo -n systemctl reset-failed ntx-soak.service 2>/dev/null  # StartLimitBurst
    sudo -n systemctl start ntx-soak.service
    sleep 8
    systemctl is-active --quiet ntx-soak.service \
      && say "soak is back up" \
      || say "*** THE SOAK IS DOWN AND I COULD NOT RESTART IT ***"
  fi
}
trap restore EXIT

# `ntx.ops.venue_state` prints "positions  N  (code=0)" per row. COUNT it -- the
# first version of this script grepped for the word "position", which matches
# "positions  0", so it announced "position open and protected" on a flat account
# and drilled nothing (R70: a check may not report success when it cannot tell).
count_of() { awk -v k="$1" '$1==k {print $2; found=1} END {if (!found) print "?"}' "$2"; }

# The demo sizing basis lives in the systemd unit, not .env, so a node started by
# hand refuses rather than sizing against ~$95k of demo play money. Use the same
# basis the soak runs at.
export NTX_EQUITY_OVERRIDE_USD=${NTX_EQUITY_OVERRIDE_USD:-1000}

# S900 enters on a bar close. On the strategy's own 4h bars that is a wait of up
# to four hours, which is why the first run opened nothing in three minutes --
# the node was healthy and simply had no bar to act on. One minute bars make the
# drill take minutes, and ONE instrument makes "exactly one position" a fact
# rather than a hope.
# 3-minute bars: S900 enters on the first bar close (~3 min) and holds for
# max_bars_in_position=3 (~9 min) -- enough life to kill, restart, adopt and
# verify. On 1-minute bars the position expired after three minutes, while the
# drill was still waiting, and the drill then blamed the venue for being flat.
DRILL_BARS=${DRILL_BARS:-3-MINUTE-LAST-EXTERNAL}
DRILL_ASSET=${DRILL_ASSET:-SOL}
NODE_ARGS=(--strategy S900 --bar-spec "$DRILL_BARS" --instruments "$DRILL_ASSET")

# ---- 1. open a real, protected position ------------------------------------ #
say "S900 on $DRILL_ASSET $DRILL_BARS: opening one demo position (sizing \$$NTX_EQUITY_OVERRIDE_USD)"
NTX_VENUE=okx_demo "$PY" -m ntx.live.node "${NODE_ARGS[@]}" > "$OUT/open.log" 2>&1 &
NODE=$!
# Ask the VENUE, not the log. The log line the drill used to wait for is written
# only when the journal is wired, so the wait always ran to its full timeout --
# by which point S900 had opened the position AND closed it again, and the drill
# reported "no position" about an account that had held one for two minutes.
# The venue is the only authority on whether a position exists (R27).
say "waiting for the position (polling the venue)"
for _ in $(seq 1 60); do
  sleep 5
  kill -0 "$NODE" 2>/dev/null || die "the node exited before it opened anything -- tail $OUT/open.log"
  "$PY" -m ntx.ops.venue_state --demo > "$OUT/1-before.txt" 2>&1
  [ "$(count_of positions "$OUT/1-before.txt")" = "1" ] && break
done
POS=$(count_of positions "$OUT/1-before.txt")
STOPS=$(count_of open_algo_conditional "$OUT/1-before.txt")
[ "$POS" = "1" ] || { kill -9 "$NODE" 2>/dev/null; die "expected 1 open position, venue says '$POS' -- see $OUT/open.log"; }
[ "$STOPS" = "1" ] || { kill -9 "$NODE" 2>/dev/null; die "expected 1 venue stop, venue says '$STOPS' -- the drill needs a PROTECTED position"; }
say "1 position open with 1 venue stop; state in 1-before.txt"

# ---- 2. kill it the way a crash would -------------------------------------- #
say "kill -9 (not SIGTERM: a crash never gets to clean up, R45)"
kill -9 "$NODE" 2>/dev/null
wait "$NODE" 2>/dev/null
"$PY" -m ntx.ops.venue_state --demo > "$OUT/2-after-kill.txt" 2>&1

# ---- 3. restart: this is the path under test -------------------------------- #
say "restarting -- adoption runs now"
NTX_VENUE=okx_demo "$PY" -m ntx.live.node "${NODE_ARGS[@]}" > "$OUT/adopt.log" 2>&1 &
NODE=$!
sleep 45
"$PY" -m ntx.ops.venue_state --demo > "$OUT/3-after-adopt.txt" 2>&1

# ---- 4. what the drill is actually asking ----------------------------------- #
FAIL=0
check() { if eval "$2"; then echo "  PASS  $1"; else echo "  FAIL  $1"; FAIL=1; fi; }
echo
echo "acceptance:"
check "the node stayed up"            "kill -0 $NODE 2>/dev/null"
check "adoption ran"                 "grep -q 'ADOPTION' '$OUT/adopt.log'"
check "the position was adopted, not flattened" "! grep -qi 'ADOPTION flatten' '$OUT/adopt.log'"
check "the position still exists"    "[ \"\$(count_of positions '$OUT/3-after-adopt.txt')\" = 1 ]"
check "exactly one stop at the venue" "[ \"\$(count_of open_algo_conditional '$OUT/3-after-adopt.txt')\" = 1 ]"
check "the stop was released once, not twice" "[ \$(grep -c 'ADOPTION AMBIGUITY' '$OUT/adopt.log') -eq 0 ]"
check "no phantom position in state"  "! grep -qi 'Cannot open NETTING' '$OUT/adopt.log'"
check "no unprotected position"       "! grep -q 'has no live protective stop' '$OUT/adopt.log'"
check "no orphan was closed"          "! grep -q 'orphan' '$OUT/adopt.log'"
check "snapshot healthy"              "'$PY' -m ntx.ops.snapshot_check '$REPO/data/state/S900-demo.json' --max-age 300"

# ---- 5. leave the account flat ---------------------------------------------- #
say "flattening"
kill -TERM "$NODE" 2>/dev/null; sleep 5; kill -9 "$NODE" 2>/dev/null
echo "FLATTEN DEMO" | "$PY" -m ntx.ops.flatten --demo > "$OUT/4-flatten.txt" 2>&1
"$PY" -m ntx.ops.venue_state --demo > "$OUT/5-final.txt" 2>&1
check "account is flat" "[ \"\$(count_of positions '$OUT/5-final.txt')\" = 0 ]"

echo
[ "$FAIL" = 0 ] && say "DRILL PASSED -- $OUT" || say "DRILL FAILED -- read $OUT"
exit "$FAIL"
