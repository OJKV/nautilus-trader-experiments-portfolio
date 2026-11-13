#!/usr/bin/env bash
# Consistent copy of every journal (sqlite3 .backup is safe while the bot writes).
# Keeps 14 days, and never fewer than KEEP_MIN copies per journal. Pull them to
# the Mac with `make pull-journal`.
#
# It must be able to FAIL (O3-08). It used to report success with zero journals
# (nullglob), never checked the copy, and pruned by age alone -- so two weeks of
# silent failures would have deleted the last good backup. Exit non-zero means
# the unit goes `failed`, its OnFailure= hook pushes, and vps_check says FAIL.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=data/backups
KEEP_DAYS=14
KEEP_MIN=3
command -v sqlite3 >/dev/null || { echo "journal backup FAILED: no sqlite3 CLI" >&2; exit 1; }
mkdir -p "$OUT"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
shopt -s nullglob
journals=(data/journal*.db)
if [ ${#journals[@]} -eq 0 ]; then
  echo "journal backup FAILED: no data/journal*.db in $(pwd) -- wrong directory?" >&2
  exit 1
fi
failed=0
for db in "${journals[@]}"; do
  name=$(basename "$db" .db)
  copy="$OUT/${name}-${STAMP}.db"
  if ! sqlite3 "$db" ".backup '$copy'" \
     || [ "$(sqlite3 "$copy" 'PRAGMA integrity_check;' 2>&1)" != "ok" ]; then
    echo "journal backup FAILED for $db" >&2
    rm -f "$copy"
    failed=1
    continue
  fi
  # Prune only this journal's copies, only past the newest KEEP_MIN, only by age.
  # `${name}-[0-9]` keeps `journal` from matching `journal-demo-S003-...`.
  old=$(ls -1t "$OUT/${name}-"[0-9]*Z.db | tail -n +$((KEEP_MIN + 1)))
  for f in $old; do
    find "$f" -mtime +"$KEEP_DAYS" -delete
  done
done
[ "$failed" -eq 0 ] || exit 1
echo "journal backup done: ${#journals[@]} journal(s), $(ls -1 "$OUT" | wc -l | tr -d ' ') file(s) kept"
