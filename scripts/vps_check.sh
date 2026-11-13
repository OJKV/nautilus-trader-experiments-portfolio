#!/usr/bin/env bash
# ntx VPS readiness check. Run ON THE SERVER:
#   scp scripts/vps_check.sh user@<server>:/tmp/ && ssh user@<server> 'bash /tmp/vps_check.sh'
# Read-only: it changes nothing. Items needing root say SUDO when run without it.
# Re-run with sudo for the full picture: ssh user@<server> 'sudo bash /tmp/vps_check.sh'
set -uo pipefail
PASS=0; FAIL=0; WARN=0
ok()   { printf "  \033[32m[ ok  ]\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31m[FAIL ]\033[0m %s\n" "$1"; FAIL=$((FAIL+1)); }
warn() { printf "  \033[33m[warn ]\033[0m %s\n" "$1"; WARN=$((WARN+1)); }
info() { printf "  [info ] %s\n" "$1"; }
sect() { printf "\n\033[1m%s\033[0m\n" "$1"; }  # not "head": that shadows /usr/bin/head
root() { [ "$(id -u)" -eq 0 ]; }
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'
NTX_HOME=${NTX_HOME:-/home/ntx}
REPO=${REPO:-$NTX_HOME/ntx}

sect "host"
. /etc/os-release 2>/dev/null && info "$PRETTY_NAME  kernel $(uname -r)  $(uname -m)"
case "$(uname -m)" in x86_64) ok "x86_64 (matches the nautilus wheel in uv.lock)";;
  *) bad "arch $(uname -m): uv.lock ships manylinux x86_64/aarch64 wheels -- verify";; esac
G=$(ldd --version 2>/dev/null | awk 'NR==1{print $NF}' | grep -oE '^[0-9]+\.[0-9]+')
awk -v g="$G" 'BEGIN{exit !(g+0 >= 2.35)}' && ok "glibc $G >= 2.35" || bad "glibc $G < 2.35: nautilus wheel will not install"
info "cpu $(nproc)  ram $(free -m | awk '/Mem:/{print $2"MB"}')  disk free $(df -h / | awk 'NR==2{print $4}')"
[ "$(free -m | awk '/Mem:/{print $2}')" -ge 3500 ] && ok "RAM >= 4 GB (two bots)" || warn "RAM < 4 GB with two bots"
D=$(df -BG / | awk 'NR==2{gsub("G","",$4); print $4}'); [ "${D:-0}" -ge 10 ] && ok "disk free ${D}G" || warn "disk free ${D}G is tight"
S=$(swapon --show=NAME --noheadings 2>/dev/null | wc -l); [ "$S" -gt 0 ] && ok "swap present" || warn "no swap (4 GB, two bots: add 2 GB)"

sect "time (signed OKX requests fail if the clock drifts)"
if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ]; then ok "clock synchronised"; else bad "clock NOT synchronised: systemctl enable --now systemd-timesyncd"; fi
info "timezone $(timedatectl show -p Timezone --value 2>/dev/null)"

sect "users"
for u in ntx argus "${NTX_ADMIN_USER:-example-operator}"; do
  if id "$u" >/dev/null 2>&1; then
    sh=$(getent passwd "$u" | cut -d: -f7)
    ok "user $u exists (shell $sh)"
  else
    [ "$u" = ntx ] && bad "user ntx missing" || info "user $u not present"
  fi
done
if id ntx >/dev/null 2>&1; then PW=$(passwd -S ntx 2>/dev/null)
  case "$PW" in *" L "*|*" NP "*) ok "ntx has no usable password (service account)";;
    *) warn "ntx password state: $PW";; esac
fi

sect "ssh"
SSHD=/etc/ssh/sshd_config
if [ -r $SSHD ]; then
  eff() { grep -hiE "^\s*$1" $SSHD /etc/ssh/sshd_config.d/*.conf 2>/dev/null | tail -1 | awk '{print tolower($2)}'; }
  [ "$(eff PermitRootLogin)" = "no" ] && ok "PermitRootLogin no" || warn "PermitRootLogin=$(eff PermitRootLogin) (root@ SSH should be off)"
  [ "$(eff PasswordAuthentication)" = "no" ] && ok "PasswordAuthentication no" || bad "PasswordAuthentication is on: keys only"
else warn "cannot read $SSHD (SUDO)"; fi

sect "firewall / fail2ban"
if root; then
  UFW=$(ufw status 2>/dev/null)
  case "$UFW" in *"Status: active"*) ok "ufw active";; *) bad "ufw inactive";; esac
  case "$UFW" in *22*|*OpenSSH*|*openssh*) ok "ufw allows SSH";; *) bad "ufw does not allow SSH";; esac
  systemctl is-active --quiet fail2ban && ok "fail2ban running" || warn "fail2ban not running"
  fail2ban-client status sshd 2>/dev/null | grep -E "Currently banned|Banned IP" | sed 's/^/        /'
else warn "ufw/fail2ban need root (SUDO): re-run with sudo"; fi
systemctl is-enabled --quiet unattended-upgrades 2>/dev/null && ok "unattended-upgrades enabled" || warn "unattended-upgrades not enabled"

sect "network: OKX keys are bound to the IPv4"
# Plain-text IP services only: ifconfig.co now answers with a Cloudflare challenge.
myip() { curl $1 -s -m 12 https://api.ipify.org 2>/dev/null || curl $1 -s -m 12 https://icanhazip.com 2>/dev/null | tr -d "\n"; }
V4=$(myip -4); info "IPv4 ${V4:-unknown}"
V6=$(myip -6); [ -n "$V6" ] && info "IPv6 $V6" || info "no IPv6 egress"
# Which family is used depends on the host: what matters is what OKX sees,
# checked against the trace below. `precedence` in gai.conf settles it globally.
if grep -qE '^[[:space:]]*precedence[[:space:]]+::ffff:0:0/96[[:space:]]+100' /etc/gai.conf 2>/dev/null
then ok "/etc/gai.conf prefers IPv4"; else warn "/etc/gai.conf has no IPv4 precedence line"; fi
# "Prefers" is not "always": on 2026-09-23 a signed call still left over IPv6 and
# OKX refused it (50110, F061). Our own REST helper now pins IPv4 (curl --ipv4),
# but the nautilus adapter's client is not ours to pin -- so if this box can reach
# OKX over IPv6 at all, that address belongs in the key's whitelist too.
if [ -n "$V6" ]; then
  C6=$(curl -6 -s -m 12 -o /dev/null -w '%{http_code}' -A "$UA" https://www.okx.com/api/v5/public/time 2>/dev/null)
  case "$C6" in
    200) warn "OKX is reachable over IPv6 ($V6): it must be on both keys' whitelists -- the accounts section below says whether it is";;
    *)   ok "no IPv6 route to OKX (nothing can leave over $V6)";;
  esac
fi

sect "okx reachability (no VPN needed here)"
T=$(curl -s -m 20 -A "$UA" https://www.okx.com/cdn-cgi/trace 2>/dev/null)
LOC=$(printf '%s' "$T" | grep -oE '^loc=.*' | cut -d= -f2)
TIP=$(printf '%s' "$T" | grep -oE '^ip=.*' | cut -d= -f2)
case "$LOC" in
  FI|SG|CH|MX|NL|NO|PL|RO|SE|DE) ok "OKX sees country=$LOC (not on the restricted list)";;
  US|CA|HK|JP|IN|MY|UZ|NG|GB|AU|FR) bad "OKX sees country=$LOC -- RESTRICTED: do not trade from here";;
  "") warn "could not read the OKX trace";;
  *) warn "OKX sees country=$LOC -- check docs/07 before trading";;
esac
[ -n "$TIP" ] && { [ "$TIP" = "$V4" ] && ok "OKX sees the IPv4 ($TIP): matches an IPv4-bound key" || bad "OKX sees $TIP but the IPv4 is $V4 -- an IPv4-bound key will be rejected"; }
C=$(curl -s -o /dev/null -w '%{http_code}' -m 20 -A "$UA" https://www.okx.com/api/v5/public/time)
[ "$C" = "200" ] && ok "https://www.okx.com public API reachable (200)" || bad "OKX public API returned $C"
C2=$(curl -s -o /dev/null -w '%{http_code}' -m 20 https://www.okx.com/api/v5/public/time)
[ "$C2" = "200" ] && info "default curl UA also accepted" || info "default UA blocked ($C2) -- expected, we send a browser UA (R25)"

sect "python / uv / repo"
UVBIN=$(command -v uv || true)
[ -x "$UVBIN" ] || UVBIN="$NTX_HOME/.local/bin/uv"   # what deploy/update.sh resolves;
[ -x "$UVBIN" ] || UVBIN="$HOME/.local/bin/uv"       # sudo's secure_path hides it
[ -x "$UVBIN" ] && ok "uv $("$UVBIN" --version 2>/dev/null | awk '{print $2}') [$UVBIN]" || bad "uv not installed (looked in PATH, $NTX_HOME/.local/bin, $HOME/.local/bin)"
if [ -d "$REPO" ]; then
  ok "repo at $REPO"
  if [ -x "$REPO/.venv/bin/python" ]; then
    ok "venv: $("$REPO/.venv/bin/python" -V 2>&1)"
    "$REPO/.venv/bin/python" -c 'import nautilus_trader as n; print(n.__version__)' >/tmp/_nt 2>/dev/null \
      && { V=$(cat /tmp/_nt); [ "$V" = "1.230.0" ] && ok "nautilus_trader $V" || bad "nautilus_trader $V (expected 1.230.0)"; } \
      || bad "nautilus_trader does not import"
    "$REPO/.venv/bin/python" -c 'import ntx' 2>/dev/null && ok "ntx package importable" || bad "ntx not importable (run make install)"
  else bad "no venv at $REPO/.venv (run make install)"; fi
  [ -f "$REPO/uv.lock" ] && ok "uv.lock present" || warn "uv.lock missing: versions will drift from the Mac"
  if [ -f "$REPO/.env" ]; then
    M=$(stat -c %a "$REPO/.env" 2>/dev/null)
    [ "$M" = "600" ] && ok ".env mode 600" || bad ".env mode $M -- chmod 600 $REPO/.env"
    grep -qE '^NTX_VENUE=okx_live' "$REPO/.env" && ok "NTX_VENUE=okx_live" || warn "NTX_VENUE is not okx_live"
    grep -qE '^OKX_BASE_URL_HTTP=https://www\.okx\.com' "$REPO/.env" && ok "auth host is www.okx.com" || bad "OKX_BASE_URL_HTTP must be https://www.okx.com"
    # Demo phase: the soak uses OKX_DEMO_API_*; the live keys are added at go-live
    # only, so their absence is expected until then (docs/21 Stage 6.2).
    for k in OKX_DEMO_API_KEY OKX_DEMO_API_SECRET OKX_DEMO_API_PASSPHRASE; do
      grep -qE "^$k=.+" "$REPO/.env" && ok "$k set (value not read)" || bad "$k missing (the demo soak needs it)"
    done
    LIVEN=0
    for k in OKX_API_KEY OKX_API_SECRET OKX_API_PASSPHRASE; do
      grep -qE "^$k=.+" "$REPO/.env" && LIVEN=$((LIVEN+1))
    done
    case "$LIVEN" in
      3) ok "live OKX_API_* set (go-live phase)";;
      0) info "live OKX_API_* empty -- expected during the demo soak";;
      *) bad "live OKX_API_* only partly set ($LIVEN/3)";;
    esac
    grep -qE "^NTX_ALERT_NTFY_URL=.+" "$REPO/.env" && ok "alert push configured" || warn "NTX_ALERT_NTFY_URL empty: no alerts will be sent (docs/23 §4)"
    grep -qE "^NTX_HEALTHCHECK_URL=.+" "$REPO/.env" && ok "dead-man switch configured" || warn "NTX_HEALTHCHECK_URL empty: nothing notices if the box dies"
  else bad "no $REPO/.env yet (copy .env.example and paste the keys)"; fi
elif [ -e "$REPO" ] || [ -e "$NTX_HOME" ]; then
  # $NTX_HOME is mode 750: another admin user sees "not found" where the truth is
  # "not mine to read". Say which, so nobody re-clones a repo that is already there.
  bad "$REPO is not readable as $(id -un) -- run: sudo -u ntx $0"
else bad "repo not found at $REPO (clone it as the ntx user)"; fi

sect "okx accounts (which account, how much, and is our IPv6 whitelisted)"
# Answers without an OKX login: which sub-account each key belongs to (ADR-0014 --
# the app opens on the MAIN account, so "$106K there, $0 here" is not a bug), what
# the account really holds, and whether a signed call is accepted when it leaves
# over IPv6 (F061). Read-only: GET only, no order and no setting changed.
# Needs .env, so this section is why the operator path is `sudo -n -u ntx $0`.
if [ ! -d "$REPO" ]; then
  warn "no repo at $REPO: cannot read the accounts"
elif [ ! -x "$REPO/.venv/bin/python" ]; then
  warn "no venv at $REPO/.venv: cannot read the accounts (run make install)"
elif [ ! -r "$REPO/.env" ]; then
  # "not readable" is not "no keys" -- a check may never report absence when it
  # means "I could not tell" (R70/F060).
  warn "$REPO/.env not readable as $(id -un): re-run as sudo -n -u ntx $0"
else
  # Not piped: $? must be the tool's own status, not sed's (R32).
  ACC=$("$REPO/.venv/bin/python" -m ntx.ops.accounts --check-ipv6 2>&1); ARC=$?
  printf '%s\n' "$ACC" | sed 's/^/        /'
  case $ARC in
    0) ok "accounts read and nothing wrong (the IPv6 whitelist check included)";;
    1) bad "ntx.ops.accounts reported a problem -- see its lines above";;
    2) warn "ntx.ops.accounts could not run (no keys, or no answer from OKX) -- unknown, not a pass";;
    *) warn "ntx.ops.accounts exited $ARC -- unknown, not a pass";;
  esac
fi

sect "services and timers"
# NEVER `systemctl ... | grep -q`: grep exits on the first match, systemctl
# gets EPIPE, and `set -o pipefail` turns a FOUND unit into "not installed".
# On the real box that answered wrongly 8 times in 15 (F060).
have_unit() { local o; o=$(systemctl list-unit-files --no-legend -- "$1" 2>/dev/null) && [ -n "$o" ]; }
u_state()   { printf '%s/%s' "$(systemctl is-enabled "$1" 2>/dev/null)" "$(systemctl is-active "$1" 2>/dev/null)"; }
# A bot unit must stop cleanly (SIGTERM, not SIGINT -- F039/R45), restart only on a
# failure, and page on a crash instead of dying quietly.
bot_props() {
  local U; U=$(systemctl cat "$1" 2>/dev/null)
  printf '%s' "$U" | grep -q 'KillSignal=SIGTERM' && ok "$1 KillSignal=SIGTERM" || warn "$1 should set KillSignal=SIGTERM"
  # `Restart=always` + `RestartPreventExitStatus=2` is the shipped contract: come
  # back from a crash, but STAY DOWN when the node refused to start (R63).
  case "$U" in
    *Restart=always*RestartPreventExitStatus=2*|*RestartPreventExitStatus=2*Restart=always*)
      ok "$1 Restart=always, and exit 2 stays down (R63)";;
    *Restart=on-failure*) ok "$1 Restart=on-failure";;
    *) warn "$1 restart policy: expected Restart=always + RestartPreventExitStatus=2";;
  esac
  printf '%s' "$U" | grep -qE '^OnFailure=.+' && ok "$1 OnFailure= set (a crash pages)" || warn "$1 has no OnFailure= -- a crash would be silent"
}
# A timer with no next elapse is installed but dead. Realtime for OnCalendar=,
# monotonic for OnUnitActiveSec=.
_elapse_seconds() {  # "5d 18h 35min 36.16s" -> 498936.16
  printf '%s' "$1" | awk '{ t=0
    for (i=1;i<=NF;i++) { v=$i; sub(/[a-z]+$/,"",v); u=$i; sub(/^[0-9.]+/,"",u)
      if (u=="d") t+=v*86400; else if (u=="h") t+=v*3600
      else if (u=="min"||u=="m") t+=v*60; else if (u=="s") t+=v
      else if (u=="ms") t+=v/1000; else if (u=="us") t+=v/1000000 }
    printf "%.0f", t }'
}
timer_next() {
  local r m secs up
  r=$(systemctl show "$1" -p NextElapseUSecRealtime --value 2>/dev/null)
  case "$r" in ""|n/a|0) ;; *) printf '%s' "$r"; return 0;; esac
  m=$(systemctl show "$1" -p NextElapseUSecMonotonic --value 2>/dev/null)
  case "$m" in ""|n/a|0) return 1;; esac
  # Monotonic counts from BOOT, not from now. Printed raw it said a 5-minute
  # timer would next fire "in 5d 18h" on a box up 5d 18h -- the exact number an
  # operator uses to tell a live timer from a dead one (F060).
  secs=$(_elapse_seconds "$m")
  up=$(awk '{print $1}' /proc/uptime 2>/dev/null || echo 0)
  awk -v s="$secs" -v u="${up:-0}" 'BEGIN { d=s-u; if (d<0) d=0
    if (d>=3600) printf "in %dh %dmin", d/3600, (d%3600)/60
    else if (d>=60) printf "in %dmin %ds", d/60, d%60
    else printf "in %ds", d }'
}

# the demo soak -- the 24/7 unit for Stages 9-12
if have_unit ntx-soak.service; then
  S=$(u_state ntx-soak.service)
  case "$S" in enabled/active*) ok "ntx-soak.service $S (demo soak)";;
    *) warn "ntx-soak.service $S -- expected enabled/active during the soak";; esac
  bot_props ntx-soak.service
else info "ntx-soak.service not installed yet (docs/21 Stage 9.1)"; fi

# the live unit -- installed at go-live only, and must not be running before it
if have_unit ntx-s003.service; then
  S=$(u_state ntx-s003.service)
  # Live since 2026-09-24: running is the normal state, anything else is news
  # (the old "running is only for after go-live" warning fired on every run, O3-10).
  case "$S" in
    */active) ok "ntx-s003.service (LIVE money) $S";;
    *) warn "ntx-s003.service (LIVE money) $S -- the live node is not running";;
  esac
  bot_props ntx-s003.service
else info "no ntx-s003.service yet (expected until go-live)"; fi

# The other long-running units. A missing one is a warning, not an absence (R70).
for s in ntx-s004-demo2.service ntx-ui.service; do
  if have_unit "$s"; then
    S=$(u_state "$s")
    case "$S" in enabled/active*) ok "$s $S";; *) warn "$s $S -- expected enabled/active";; esac
  else warn "$s not installed"; fi
done

# oneshot workers: inactive between runs is normal -- the timer is what must be
# live. But `failed` is the last run failing: it was printed as [ ok ] (O3-08).
for s in ntx-alert.service ntx-journal-backup.service; do
  if have_unit "$s"; then
    S=$(u_state "$s")
    case "$S" in */failed) bad "$s last run FAILED ($S) -- journalctl -u $s";;
      *) ok "$s installed ($S)";; esac
  else info "$s not installed yet (docs/21 Stage 9)"; fi
done
have_unit 'ntx-alert-crash@.service' \
  && ok "ntx-alert-crash@.service installed (the OnFailure= target)" \
  || info "ntx-alert-crash@.service not installed yet -- a crash would not page"

for t in ntx-alert.timer ntx-journal-backup.timer ntx-update.timer; do
  if have_unit "$t"; then
    S=$(u_state "$t")
    case "$S" in enabled/active*) ok "$t $S";; *) warn "$t $S -- expected enabled/active";; esac
    if N=$(timer_next "$t"); then ok "$t scheduled (next $N)"; else warn "$t is installed but has no next run scheduled"; fi
  else info "$t not installed yet (docs/21 Stage 9)"; fi
done
systemctl list-unit-files 2>/dev/null | grep -qE '^argus.*\.service' && info "ARGUS units present (shared box)" || true

sect "the bot itself (what the units cannot tell you)"
# A unit can be active while the node is frozen, disconnected, unwarmed, KILL-latched
# or holding an unprotected position. That question has one answer: the snapshot the
# node rewrites every 15 s -- the same check the deploy gates on.
if [ -x "$REPO/.venv/bin/python" ] && [ -d "$REPO/data/state" ]; then
  SNAPS=$(find "$REPO/data/state" -name '*.json' 2>/dev/null | sort)
  [ -n "$SNAPS" ] || info "no state snapshot yet (the node has not started)"
  for SNAP in $SNAPS; do
    OUT=$("$REPO/.venv/bin/python" -m ntx.ops.snapshot_check "$SNAP" --max-age 120 2>&1); RC=$?
    case $RC in
      0) ok "$(basename "$SNAP"): $OUT";;
      2) warn "$(basename "$SNAP"): $OUT";;
      *) bad "$(basename "$SNAP"): $OUT";;
    esac
    "$REPO/.venv/bin/python" -c '
import json, sys, time
s = json.loads(open(sys.argv[1]).read())
print("age %.0fs | %s | account $%s | sizing $%s | open %d | unprotected %s | killed %s"
      % (time.time() - s["ts_ns"] / 1e9, "DEMO" if s.get("is_demo") else "LIVE",
         s.get("account_equity_usd"), s.get("sizing_equity_usd"),
         len(s.get("positions") or []), s.get("positions_unprotected"), s.get("killed")))
for p in s.get("positions") or []:
    print("  %s %s qty=%s protected=%s" % (p.get("instrument_id") or p.get("instrument"),
                                           p.get("direction"),
                                           p.get("qty"), p.get("protected")))
' "$SNAP" 2>/dev/null | sed 's/^/        /'
  done
else info "no venv or no data/state: nothing to read"; fi

sect "alerter coverage (NTX_ALERT_BOTS vs the bots that actually write state)"
# A watched name with no snapshot pages "missing" forever; a bot that writes a
# snapshot but is not in the list is watched by nobody -- docs/26 once named
# `S004-demo2` while the bot wrote `S004-demo` (O3-05 / E3-01, R70).
if [ -x "$REPO/.venv/bin/python" ]; then
  # Only this one setting is printed; nothing key-shaped is read out (R1).
  BOTS=$(cd "$REPO" && "$REPO/.venv/bin/python" -c \
    'from ntx.config.settings import load; print(load().alert_bots)' 2>/dev/null) \
    || { bad "could not read NTX_ALERT_BOTS (settings failed to load)"; BOTS=""; }
  BOTS=$(printf '%s' "$BOTS" | tr ',' ' ')
  [ -n "$BOTS" ] || bad "NTX_ALERT_BOTS is empty -- no bot is watched"
  for b in $BOTS; do
    if [ -f "$REPO/data/state/$b.json" ]; then ok "watched: $b (snapshot present)"
    else bad "NTX_ALERT_BOTS names $b but data/state/$b.json does not exist"; fi
  done
  # Every snapshot updated in the last day must be watched.
  for SNAP in $(find "$REPO/data/state" -maxdepth 1 -name '*.json' -mmin -1440 2>/dev/null | sort); do
    b=$(basename "$SNAP" .json)
    case " $BOTS " in *" $b "*) ;; *) bad "$b writes a snapshot but is NOT in NTX_ALERT_BOTS";; esac
  done
else info "no venv: cannot read NTX_ALERT_BOTS"; fi

sect "journal backups"
# The backup can fail without anyone noticing; its age is the proof it runs (O3-08).
if [ -d "$REPO/data/backups" ]; then
  for db in "$REPO"/data/journal*.db; do
    [ -e "$db" ] || continue
    name=$(basename "$db" .db)
    NEWEST=$(ls -1t "$REPO/data/backups/${name}-"[0-9]*Z.db 2>/dev/null | sed -n 1p)
    if [ -z "$NEWEST" ]; then bad "$name: no backup at all"
    elif [ -n "$(find "$NEWEST" -mmin +2160 2>/dev/null)" ]; then
      bad "$name: newest backup is older than 36 h ($(basename "$NEWEST"))"
    else ok "$name: newest backup $(basename "$NEWEST")"; fi
  done
else bad "no data/backups directory -- the journal has never been backed up"; fi

printf "\n\033[1m%s\033[0m\n" "$PASS ok | $FAIL FAIL | $WARN warn"
[ "$FAIL" -eq 0 ] || exit 1
