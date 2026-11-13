# systemd units for the VPS

Copy to `/etc/systemd/system/`, edit the paths if the repo is not `/home/ntx/ntx`,
then `sudo systemctl daemon-reload`. Details and the order to enable them in:
`docs/21-hetzner-setup-guide.md` (Stage 9) and `docs/23-live-observability.md` (§4).

| Unit | What it does | Enable when |
|---|---|---|
| `ntx-soak.service` | S003 on the OKX **demo** account, 24/7 | now — it is the soak |
| `ntx-s003.service` | S003 on the **live** account | only after the go-live checklist |
| `ntx-alert.service` + `.timer` | every 5 min: journal criticals, stale snapshot → push + dead-man ping | with the first bot |
| `ntx-journal-backup.service` + `.timer` | nightly `sqlite3 .backup` of every journal, 14 kept | with the first bot |

`OnFailure=ntx-alert-crash@%n.service` makes a crash page you instead of dying quietly.
