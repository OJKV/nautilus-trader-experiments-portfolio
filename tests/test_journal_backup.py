"""R-007 O3-08: the journal backup could not fail, and could prune itself to zero.

`nullglob` + `set -e` made zero journals a success; the copy was never checked;
and `find -mtime +14 -delete` would, after two weeks of failures, delete the
last good copy. The journal is the one irreplaceable file on the box (R29).

Run in a throwaway tree: never against data/ (R29).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(
    os.environ.get("NTX_BACKUP_SCRIPT") or Path(__file__).parents[1] / "scripts/journal_backup.sh"
)

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="needs the sqlite3 CLI")


def _tree(tmp_path) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "data").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts/journal_backup.sh")
    return tmp_path / "scripts/journal_backup.sh"


def _run(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script)], capture_output=True, text=True)


def _journal(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()


def test_no_journal_at_all_is_a_failure(tmp_path):
    assert _run(_tree(tmp_path)).returncode != 0, "backed up nothing and called it success"


def test_a_good_journal_is_copied_and_verified(tmp_path):
    script = _tree(tmp_path)
    _journal(tmp_path / "data/journal.db")
    r = _run(script)
    assert r.returncode == 0, r.stderr
    copies = list((tmp_path / "data/backups").glob("journal-*.db"))
    assert len(copies) == 1
    assert sqlite3.connect(copies[0]).execute("SELECT x FROM t").fetchone() == (1,)


def _old_copies(tmp_path, name="journal", n=5) -> list[Path]:
    out = tmp_path / "data/backups"
    out.mkdir(parents=True, exist_ok=True)
    old = time.time() - 20 * 86400
    paths = []
    for i in range(n):
        p = out / f"{name}-202609{10 + i:02d}T000000Z.db"
        _journal(p)
        os.utime(p, (old + i, old + i))
        paths.append(p)
    return paths


def test_pruning_never_goes_below_the_minimum(tmp_path):
    script = _tree(tmp_path)
    _journal(tmp_path / "data/journal.db")
    _old_copies(tmp_path)
    assert _run(script).returncode == 0
    left = list((tmp_path / "data/backups").glob("journal-[0-9]*Z.db"))
    assert len(left) >= 3, f"pruned to {len(left)} copies"


def test_a_failed_backup_fails_loudly_and_prunes_nothing(tmp_path):
    script = _tree(tmp_path)
    (tmp_path / "data/journal.db").write_bytes(b"not a database at all" * 100)
    old = _old_copies(tmp_path)
    assert _run(script).returncode != 0, "a corrupt journal was 'backed up'"
    assert all(p.exists() for p in old), "a failing run deleted good old copies"


# ------------------------------------------------------------ units, O2-06 -- #
DEPLOY = Path(__file__).parents[1] / "deploy"


def test_a_failed_backup_pages_and_cannot_hang():
    unit = (DEPLOY / "ntx-journal-backup.service").read_text()
    assert "OnFailure=ntx-alert-crash@%n.service" in unit, "a failed backup must push"
    assert "TimeoutStartSec=" in unit


def test_the_alerter_cannot_hang_its_own_timer():
    unit = (DEPLOY / "ntx-alert.service").read_text()
    assert "TimeoutStartSec=" in unit, "a oneshot has no start timeout by default"
    # F052: the alerter must never alert about itself through systemd.
    assert "OnFailure=" not in unit.split("[Service]")[0].replace("# No OnFailure=", "")


# --------------------------------------------------- alerter, O3-08 / 5C -- #
def test_a_journal_never_backed_up_is_alerted(tmp_path):
    from ntx.obs.alerts import backup_alerts

    _journal(tmp_path / "journal.db")
    got = backup_alerts(tmp_path, [tmp_path / "journal.db"])
    assert [a.kind for a in got] == ["backup_stale"]


def test_a_fresh_backup_is_quiet_and_a_stale_one_is_not(tmp_path):
    from ntx.obs.alerts import BACKUP_STALE_AFTER_S, backup_alerts

    _journal(tmp_path / "journal.db")
    (tmp_path / "backups").mkdir()
    copy = tmp_path / "backups/journal-20261007T021700Z.db"
    _journal(copy)
    # A demo journal's copies must not count for the live one.
    _journal(tmp_path / "backups/journal-demo-S003-20261007T021700Z.db")
    assert backup_alerts(tmp_path, [tmp_path / "journal.db"]) == []
    old = time.time() - BACKUP_STALE_AFTER_S - 60
    os.utime(copy, (old, old))
    assert [a.kind for a in backup_alerts(tmp_path, [tmp_path / "journal.db"])] == ["backup_stale"]
