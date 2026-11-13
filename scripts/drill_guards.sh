#!/usr/bin/env bash
# Loss-guard drill: make the drawdown KILL actually fire, on a real position.
#
# The guards -- daily-loss PAUSE and drawdown KILL -- have never fired anywhere.
# They cannot fire on the soak: NTX_EQUITY_OVERRIDE_USD makes sizing equity a
# CONSTANT, so day-start == high-water == equity and the drawdown is always zero.
# That means live would be the first place either one ever runs, on the two
# mechanisms that already misfired twice in review (F056, F057). R50 asks for
# them to be drilled, not just unit-tested.
#
# The override is also what makes the drill possible: it is a number we control,
# so running at $1,000 and then restarting at $700 is a 30% drawdown as far as
# the guard is concerned, against a REAL open position that it must really close.
#
# DEMO ONLY.
#   sudo -u ntx /home/ntx/ntx/scripts/drill_guards.sh
set -uo pipefail
REPO=${REPO:-/home/ntx/ntx}
PY="$REPO/.venv/bin/python"
cd "$REPO" || { echo "no repo at $REPO"; exit 2; }

say() { printf '%s  %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { printf '\nDRILL ABORTED: %s\n' "$*" >&2; exit 1; }
count_of() { awk -v k="$1" '$1==k {print $2; found=1} END {if (!found) print "?"}' "$2"; }

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
OUT="$REPO/reports/rehearsals/$STAMP-guard-drill"
mkdir -p "$OUT"
exec > >(tee "$OUT/drill.log") 2>&1
say "loss-guard drill, demo account, $STAMP"

HIGH=${HIGH:-1000}
LOW=${LOW:-700}          # 30% below HIGH, against NTX_MAX_DRAWDOWN_EQUITY_PCT=20
STATE="$REPO/data/state/S900-demo.json"
LATCH="$STATE.killed"
# 1-minute bars: equity is journalled once per bar (the arrival of bar T closes
# the books for T-1), so the high-water mark the guard seeds from does not exist
# until at least TWO bars have gone by. On 3-minute bars the first run wrote zero
# equity rows, the mark seeded from nothing, and the drill concluded the guard was
# broken when its own precondition had never been met.
ARGS=(--strategy S900 --bar-spec 1-MINUTE-LAST-EXTERNAL --instruments SOL)
JOURNAL="$REPO/data/journal-demo-S900.db"
equity_rows() {
  "$PY" - "$JOURNAL" <<'PY' 2>/dev/null || echo 0
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1])
    print(c.execute("select count(*) from equity").fetchone()[0])
except Exception:
    print(0)
PY
}
equity_max() {
  "$PY" - "$JOURNAL" <<'PY' 2>/dev/null || echo 0
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1])
    print(c.execute("select coalesce(max(equity_usd), 0) from equity").fetchone()[0])
except Exception:
    print(0)
PY
}

SOAK_WAS_UP=0
if systemctl is-active --quiet ntx-soak.service; then
  SOAK_WAS_UP=1; sudo -n systemctl stop ntx-soak.service || die "could not stop ntx-soak"
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

  say "cleaning up: clearing any latch, restarting the soak"
  NTX_UNKILL_YES="RESUME TRADING" "$PY" -m ntx.ops.unkill --clear --bot S003-demo --equity "$HIGH" \
    --note "guard drill $STAMP" >> "$OUT/cleanup.txt" 2>&1
  rm -f "$LATCH"
  if [ "$SOAK_WAS_UP" = 1 ]; then
    # Five stop/start cycles inside StartLimitIntervalSec trip StartLimitBurst and
    # every later start is refused until the failure is cleared. Clear first, then
    # start, then CHECK -- a warning nobody reads is how the soak stayed down for
    # twenty minutes on 2026-09-24.
    sudo -n systemctl reset-failed ntx-soak.service 2>/dev/null
    sudo -n systemctl start ntx-soak.service
    sleep 8
    if systemctl is-active --quiet ntx-soak.service; then
      say "soak is back up"
    else
      say "*** THE SOAK IS DOWN AND I COULD NOT RESTART IT ***"
      say "    sudo systemctl reset-failed ntx-soak.service && sudo systemctl start ntx-soak.service"
    fi
  fi
}
trap restore EXIT

rm -f "$LATCH"

# ---- 1. a real position, and a high-water mark at $HIGH --------------------- #
say "opening a position with sizing basis \$$HIGH (this sets the high-water mark)"
NTX_EQUITY_OVERRIDE_USD=$HIGH NTX_VENUE=okx_demo "$PY" -m ntx.live.node "${ARGS[@]}" \
  > "$OUT/open.log" 2>&1 &
NODE=$!
# Wait for BOTH preconditions: a real position, and at least one journalled
# equity sample for the mark to seed from. Measure them; do not assume either.
for _ in $(seq 1 60); do
  sleep 5
  kill -0 "$NODE" 2>/dev/null || die "the node exited before opening anything -- tail $OUT/open.log"
  "$PY" -m ntx.ops.venue_state --demo > "$OUT/1-before.txt" 2>&1
  [ "$(count_of positions "$OUT/1-before.txt")" = "1" ] && break
done
[ "$(count_of positions "$OUT/1-before.txt")" = "1" ] || die "no position opened"
# The bot journals equity once per bar, and not until a bar has closed the books
# on the previous one -- measured at six minutes after entry on 1-minute bars,
# by which time S900's three-bar life is over. The position and the mark cannot
# coexist on that timing, so the drill writes the mark itself: one equity row at
# $HIGH, exactly the value and the shape the node writes (measured: 1000.0). The
# only thing simulated is "this bot has been running a while", which is true of
# every real bot. Everything after this line is the real mechanism.
if [ "$(equity_rows)" -lt 1 ]; then
  say "no equity sample yet; writing the high-water mark the node would have written"
  "$PY" - "$JOURNAL" "$HIGH" <<'PY'
import sqlite3, sys, datetime as dt
db, high = sys.argv[1], float(sys.argv[2])
c = sqlite3.connect(db)
c.execute(
    "INSERT OR REPLACE INTO equity (ts, equity_usd, unrealised, open_positions) VALUES (?,?,?,?)",
    (dt.datetime.now(dt.UTC).isoformat(), high, 0.0, 1),
)
c.commit()
PY
fi
ROWS=$(equity_rows); MARK=$(equity_max)
[ "$ROWS" -ge 1 ] || die "no equity sample and none could be written -- the drill would prove nothing"
say "1 position open; $ROWS equity sample(s), high-water mark \$$MARK"
awk -v m="$MARK" -v h="$HIGH" 'BEGIN{exit !(m > h*0.9 && m < h*1.1)}' \
  || die "the mark read back is \$$MARK, not the sizing basis \$$HIGH -- the premise is wrong"
kill -9 "$NODE" 2>/dev/null; wait "$NODE" 2>/dev/null

# ---- 2. restart poorer: the guard must KILL --------------------------------- #
say "restarting with sizing basis \$$LOW -- a $(( (HIGH-LOW)*100/HIGH ))% drawdown against the mark"
NTX_EQUITY_OVERRIDE_USD=$LOW NTX_VENUE=okx_demo "$PY" -m ntx.live.node "${ARGS[@]}" \
  > "$OUT/kill.log" 2>&1 &
NODE=$!
for _ in $(seq 1 36); do
  sleep 5
  grep -qi "KILL" "$OUT/kill.log" && break
done
sleep 20
"$PY" -m ntx.ops.venue_state --demo > "$OUT/2-after-kill.txt" 2>&1

FAIL=0
check() { if eval "$2"; then echo "  PASS  $1"; else echo "  FAIL  $1"; FAIL=1; fi; }
echo
echo "acceptance:"
check "the KILL fired"                  "grep -qi 'KILL' '$OUT/kill.log'"
check "it closed the real position"     "[ \"\$(count_of positions '$OUT/2-after-kill.txt')\" = 0 ]"
check "it left no orphan stop"          "[ \"\$(count_of open_algo_conditional '$OUT/2-after-kill.txt')\" = 0 ]"
check "the latch is on disk"            "[ -f '$LATCH' ]"
check "the snapshot says killed"        "grep -q '\"killed\": *true' '$STATE'"
check "a killed bot is not healthy"     "! '$PY' -m ntx.ops.snapshot_check '$STATE' --max-age 600"
kill -TERM "$NODE" 2>/dev/null; sleep 5; kill -9 "$NODE" 2>/dev/null

# ---- 3. the latch must survive a restart ----------------------------------- #
say "restarting again: a latched KILL must still block entries"
NTX_EQUITY_OVERRIDE_USD=$HIGH NTX_VENUE=okx_demo "$PY" -m ntx.live.node "${ARGS[@]}" \
  > "$OUT/latched.log" 2>&1 &
NODE=$!
sleep 90
"$PY" -m ntx.ops.venue_state --demo > "$OUT/3-latched.txt" 2>&1
check "still latched after a restart"   "[ -f '$LATCH' ]"
check "it opened nothing while latched" "[ \"\$(count_of positions '$OUT/3-latched.txt')\" = 0 ]"
kill -TERM "$NODE" 2>/dev/null; sleep 5; kill -9 "$NODE" 2>/dev/null

# ---- 4. only a human clears it --------------------------------------------- #
NTX_UNKILL_YES="RESUME TRADING" "$PY" -m ntx.ops.unkill --clear --bot S003-demo --equity "$HIGH" \
  --note "guard drill $STAMP" > "$OUT/4-unkill.txt" 2>&1
check "ops.unkill clears the latch"     "[ ! -f '$LATCH' ]"

echo
[ "$FAIL" = 0 ] && say "GUARD DRILL PASSED -- $OUT" || say "GUARD DRILL FAILED -- read $OUT"
exit "$FAIL"
