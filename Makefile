.PHONY: help install doctor test lint data backtest sweep paper live ui clean ui-vps ui-snapshot soak-start soak-stop soak-status alerts logs pull-journal pull-journal-sudo vps-key-ntx deploy deploy-dry deploy-live migrate-live-checkout vps-status vps-logs
PY := .venv/bin/python
-include docs/private/Makefile.local
# The VPS. Override on the command line if the alias differs: make deploy SSH_HOST=...
SSH_HOST ?= example-host
SSH_HOST_NTX ?= example-host
# public half of the key in ~/.ssh/config for both hosts (used by `make vps-key-ntx`)
SSH_PUBKEY ?= $(HOME)/.ssh/id_ed25519.pub

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t22

install:  ## create venv and install exactly the versions in uv.lock
	uv sync --frozen --python 3.12 --all-extras

lock-upgrade:  ## deliberately move dependencies forward (then run tests)
	uv lock --upgrade

doctor:   ## versions, venue reachability, key presence, account parity
	$(PY) -m ntx.doctor

vpn-check: ## is the VPN exit country allowed for OKX? run BEFORE any auth call
	$(PY) -m ntx.config.geo

doctor-vpn: vpn-check ## doctor incl. authenticated OKX checks (VPN must be ON)
	$(PY) -m ntx.doctor --auth

data:     ## build the backtest catalog (full universe, 2020 -> now)
	$(PY) -m ntx.data

data-sample: ## quick pipeline check: 2 assets, 3 months
	$(PY) -m ntx.data --assets BTC SOL --start 2024-01 --end 2024-03

data-report: ## verify the catalog by reading it back; writes reports/
	$(PY) -m ntx.data.report

review:   ## offline review of the trade journal (no network, works VPN-off)
	$(PY) -m ntx.obs.review --trades 20

ui:       ## NTX Monitor on THIS machine's data, read-only, http://127.0.0.1:8787
	$(PY) -m ntx.ui

ui-vps:   ## tunnel to the NTX Monitor running on the VPS -> http://127.0.0.1:8788
	@# This used to BE the monitor: it ssh'd in, killed whatever held the port and
	@# ran uvicorn in the foreground of that session. So the dashboard lived
	@# exactly as long as the terminal, which is why "the UI is down" was reported
	@# four times in two days while every bot was healthy. Worse, a copy started
	@# this way held 8788 against ntx-ui.service on 2026-09-27: the unit burned its
	@# start limit and sat `failed` for sixteen hours, hidden because the stray
	@# process kept serving the page.
	@#
	@# The monitor is ntx-ui.service now. A tunnel forwards a port; it does not own
	@# the thing on the other end.
	@ssh $(SSH_HOST_NTX) 'curl -sf -m 5 -o /dev/null http://127.0.0.1:8788/api/bots' \
	  || { echo "The monitor is not answering on the box -- the tunnel would show nothing."; \
	       echo "  check:   ssh $(SSH_HOST) 'systemctl status ntx-ui.service'"; \
	       echo "  restart: ssh $(SSH_HOST) 'sudo systemctl restart ntx-ui.service'"; \
	       exit 1; }
	@echo "monitor is up on the box. Tunnelling -> http://127.0.0.1:8788"
	@echo "(Ctrl-C closes the tunnel only. The monitor keeps running and survives reboots.)"
	@ssh -N -L 8788:127.0.0.1:8788 $(SSH_HOST_NTX)

ui-snapshot: ## pull the VPS data here, then view it offline (no tunnel, safe to demo)
	@$(MAKE) --no-print-directory pull-journal
	@echo "starting the monitor on the pulled copy -> http://127.0.0.1:8787"
	@NTX_DATA_DIR=data/from-vps $(PY) -m ntx.ui

runs:     ## list recorded backtest runs with their lineage
	$(PY) -m ntx.experiments.runs

verify:   ## re-run the S003 gate experiment end to end
	$(PY) -m ntx.experiments.s003_gate

verify-control: ## re-run the random-entry control (slow, 80 backtests)
	$(PY) -m ntx.experiments.random_control

mutation: ## mutation testing -- break the code, check the tests notice
	$(PY) -m ntx.qa.mutation --max 12

mutation-full: ## slower, more mutants per module
	$(PY) -m ntx.qa.mutation --max 40

test:     ## unit + integration (excludes live/vpn)
	$(PY) -m pytest

test-vpn: ## tests that need the VPN on
	$(PY) -m pytest -m vpn

lint:     ## ruff check + format check
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

fmt:      ## ruff format
	$(PY) -m ruff format src tests

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__

soak-start: ## start the S003 demo soak (VPN on)
	scripts/soak.sh start

soak-stop: ## stop the S003 demo soak cleanly
	scripts/soak.sh stop

soak-status: ## soak process, recent events, demo positions
	scripts/soak.sh status

backtest: ## S003 in/out-of-sample gate (the numbers quoted in the docs)
	.venv/bin/python -m ntx.experiments.s003_gate

cost-stress: ## S003 under 5bps slippage and 2x fees
	.venv/bin/python -m ntx.experiments.cost_stress

control: ## random-entry control, 40 seeds
	.venv/bin/python -m ntx.experiments.random_control --seeds 40

sweep: ## parameter sweeps have no CLI yet -- fail loudly instead of pretending
	@echo "no sweep CLI: use ntx.backtest.sweep from an experiment in src/ntx/experiments/" >&2
	@exit 1

paper: ## S003 on OKX demo (VPN on); manage with soak-status / soak-stop
	scripts/soak.sh start

live: ## refuse by default -- real money is never one make target away
	@echo "live trading is deliberately not a make target." >&2
	@echo "VPS: sudo systemctl start ntx-s003   (see docs/21-hetzner-setup-guide.md)" >&2
	@echo "local observe-only: .venv/bin/python -m ntx.live.node --strategy S003 --observe" >&2
	@exit 1

alerts: ## one alerter pass (prints; sends only if NTX_ALERT_NTFY_URL is set)
	.venv/bin/python -m ntx.obs.alerts --dry-run

logs: ## tail the JSON logs, human readable (needs jq)
	@ls -t logs/ntx-*.jsonl 2>/dev/null | head -1 | xargs -I{} sh -c \
	'tail -f {} | jq -r "[.timestamp[11:19], .level, .component, .message] | @tsv"'

deploy: ## push-button deploy on the VPS (pulls origin/main, tests, restarts what is safe)
	@ssh $(SSH_HOST) 'sudo -n -u ntx /home/ntx/ntx/deploy/update.sh'

deploy-live: ## deploy to the LIVE node (refuses unless the account is flat; types a confirmation)
	@# The whole command runs as ntx. It used to be `sudo -n -u ntx test -x ... && ...`,
	@# where the && binds outside sudo: only `test -x` ran as ntx and the script itself
	@# ran as the admin user, who cannot read /home/ntx (mode 750). So this target has
	@# never worked -- you could not ship a fix to a running live node (L2-05 review).
	@# Three phases. The bot user checks, tests and installs; the ADMIN account
	@# stops and starts the live unit, because a push to origin/main runs as the
	@# bot user and must not be able to control real-money trading (L2-01).
	@# The start runs even if the sync failed, so the bot always comes back.
	@ssh -t $(SSH_HOST) 'set -e; \
	  sudo -n -u ntx /home/ntx/ntx/deploy/deploy-live.sh check; \
	  sudo -n systemctl stop ntx-s003.service; \
	  sudo -n -u ntx /home/ntx/ntx/deploy/deploy-live.sh sync || echo "SYNC FAILED -- starting the old code back up"; \
	  sudo -n systemctl start ntx-s003.service; \
	  sudo -n -u ntx /home/ntx/ntx/deploy/deploy-live.sh verify'

deploy-dry: ## show what a deploy would do, change nothing
	@ssh $(SSH_HOST) 'sudo -n -u ntx /home/ntx/ntx/deploy/update.sh --dry-run'

migrate-live-checkout: ## ONE TIME: move the live node into its own checkout (docs/24 §1b)
	@# Not passwordless and not in sudoers on purpose: this runs once, it creates
	@# a checkout, and it ends with the admin installing a unit file -- none of
	@# that belongs in a NOPASSWD grant. `ssh -t` so sudo can ask for an administrator's password.
	@# The live unit must already be STOPPED with the account flat: run
	@# `make deploy-live` first, or stop it by hand after checking venue_state.
	@echo "This stops nothing by itself. The live unit must already be down."
	@ssh -t $(SSH_HOST) 'set -e; \
	  sudo -u ntx /home/ntx/ntx/scripts/setup_live_checkout.sh; \
	  sudo install -m 644 /home/ntx/ntx/deploy/*.service /home/ntx/ntx/deploy/*.timer /etc/systemd/system/; \
	  sudo systemctl daemon-reload; \
	  sudo systemctl start ntx-s003.service; \
	  sudo -n -u ntx /home/ntx/ntx/deploy/deploy-live.sh verify'

vps-status: ## units, timers and pending commits on the VPS
	@ssh $(SSH_HOST) 'systemctl list-timers "ntx-*" --no-pager | head -5; echo; \
	  systemctl is-active ntx-soak.service ntx-s003.service ntx-alert.timer ntx-update.timer \
	    | paste -d" " <(printf "soak\nlive\nalert.timer\nupdate.timer\n") -; echo; \
	  systemctl status ntx-soak.service --no-pager 2>/dev/null | head -6'
	@echo "--- checkouts (demo = the soak, live = the money node; docs/24 §1b) ---"
	@ssh $(SSH_HOST) 'sudo -n -u ntx /home/ntx/ntx/deploy/deploy-live.sh status' || true
	@echo "--- pending commits for the DEMO checkout (the deploy script is the only thing allowed to read it) ---"
	@ssh $(SSH_HOST) 'sudo -n -u ntx /home/ntx/ntx/deploy/update.sh --dry-run' || true

vps-logs: ## tail the soak journal on the VPS (N=200)
	@ssh $(SSH_HOST) 'journalctl -u ntx-soak.service -n $(or $(N),200) --no-pager'

pull-journal: ## copy the VPS journals + backups + state to ./data/from-vps (no tty, no password)
	@mkdir -p data/from-vps
	@rsync -az --partial -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' \
	  --include='journal*.db' --include='backups/***' --include='state/***' \
	  --exclude='*' $(or $(VPS),$(SSH_HOST_NTX)):ntx/data/ data/from-vps/ > /tmp/ntx-pull.log 2>&1; \
	  rc=$$?; \
	  if [ $$rc -ne 0 ]; then \
	    tail -5 /tmp/ntx-pull.log; \
	    echo "pull failed (rc=$$rc). If this is 'Permission denied (publickey)', the 'ntx'"; \
	    echo "account has no copy of this Mac's key yet. One-time fix (asks for an administrator's sudo"; \
	    echo "password once, docs/21 Stage 1.7):"; \
	    echo "  make vps-key-ntx"; \
	    exit $$rc; \
	  fi
	@echo "pulled into data/from-vps -- review the newest CONSISTENT snapshot (sqlite3 .backup):"
	@echo "  $(PY) -m ntx.obs.review --trades 20 --db $$(ls -t data/from-vps/backups/journal-demo-S003-*.db 2>/dev/null | head -1)"
	@echo "  NTX_DATA_DIR=data/from-vps make ui      # the monitor, on VPS data"

vps-key-ntx: ## RUN ON THE MAC: authorise this Mac's key for the bot account (asks for an administrator's sudo password)
	@test -f $(SSH_PUBKEY) || { \
	  echo "no public key at $(SSH_PUBKEY)"; \
	  echo "This target runs on YOUR MAC, not on the server: it copies the Mac's"; \
	  echo "public key up to the VPS. On the server there is no such key, which is"; \
	  echo "why you are seeing this message there."; \
	  exit 1; }
	scp $(SSH_PUBKEY) $(SSH_HOST):/tmp/mac.pub
	ssh -t $(SSH_HOST) 'sudo install -d -m 700 -o ntx -g ntx /home/ntx/.ssh && \
	  sudo sh -c "grep -qxFf /tmp/mac.pub /home/ntx/.ssh/authorized_keys 2>/dev/null || \
	  cat /tmp/mac.pub >> /home/ntx/.ssh/authorized_keys" && \
	  sudo chown ntx:ntx /home/ntx/.ssh/authorized_keys && \
	  sudo chmod 600 /home/ntx/.ssh/authorized_keys && rm -f /tmp/mac.pub && \
	  sudo ssh-keygen -lf /home/ntx/.ssh/authorized_keys'
	@ssh -o BatchMode=yes -o ConnectTimeout=10 $(SSH_HOST_NTX) 'echo "OK: unattended login works as $$(id -un)"'

pull-journal-sudo: ## fallback: pull through the admin account (needs a tty for the sudo password)
	@mkdir -p data/from-vps
	# The privileged step and the binary transfer are SEPARATE on purpose: a pty
	# (-t) is needed for the password prompt, but a pty mangles binary (NL->CRLF)
	# and merges the prompt into stdout. So: tar to a file on the server, then scp.
	ssh -t $(SSH_HOST) 'sudo -u ntx tar -czf /tmp/ntx-journal.tgz -C /home/ntx/ntx/data \
	  journal-demo-S003.db backups state && sudo chmod 0644 /tmp/ntx-journal.tgz'
	scp $(SSH_HOST):/tmp/ntx-journal.tgz /tmp/ntx-journal.tgz
	@ssh -o BatchMode=yes $(SSH_HOST) 'rm -f /tmp/ntx-journal.tgz'
	@test -s /tmp/ntx-journal.tgz || { echo "nothing came back"; exit 1; }
	tar -xzf /tmp/ntx-journal.tgz -C data/from-vps
	@echo "pulled into data/from-vps"
