"""`deploy-live.sh check` must not open the door inside the bar window (L3-06, D3-04).

R65 / F054: a restart replays history WITHOUT trading, so a node that is down
across a 4h close, or restarted in the minutes after one, silently drops that
bar's entry or exit. The demo timer has waited out that window since 2026-09-20;
the money node did not. `make deploy-live` at 15:58 UTC stopped `ntx-s003`
across the 16:00 close whenever the venue read and the typed phrase passed.

`check` is the last thing that runs before the admin's `systemctl stop`, so it
is where the window is asked -- after the confirmation, so a slow human at the
prompt cannot drift into it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "deploy-live.sh"

# The live checkout's python is the OLD live code: it may not know `--before`.
LIVE_PY = r"""#!/usr/bin/env bash
echo "live-python $*" >> "$FAKE_LOG"
case "$*" in
  *ntx.ops.venue_state*) exit 0 ;;          # flat
  *ntx.ops.deploy_window*) exit 2 ;;        # argparse: unrecognised --before
  *) exit 0 ;;
esac
"""
DEMO_PY = r"""#!/usr/bin/env bash
echo "demo-python $*" >> "$FAKE_LOG"
case "$*" in
  *ntx.ops.deploy_window*) exit "${FAKE_WINDOW_RC:-0}" ;;
  *) exit 0 ;;
esac
"""


def _exe(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    p.chmod(0o755)


def _check(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, str]:
    live, demo, log = tmp_path / "live", tmp_path / "demo", tmp_path / "calls.log"
    (live / ".git").mkdir(parents=True, exist_ok=True)
    _exe(live / ".venv" / "bin" / "python", LIVE_PY)
    _exe(demo / ".venv" / "bin" / "python", DEMO_PY)
    log.write_text("")
    r = subprocess.run(
        ["bash", str(SCRIPT), "check", "--yes"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "NTX_LIVE_REPO": str(live),
            "NTX_DEMO_REPO": str(demo),
            "NTX_DATA_DIR": str(tmp_path / "data"),
            "FAKE_LOG": str(log),
            **env,
        },
    )
    return r, log.read_text()


def test_outside_the_window_the_check_passes(tmp_path):
    """The positive case (R70): a refusal proven only in the negative is not proven."""
    r, calls = _check(tmp_path, FAKE_WINDOW_RC="0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "checks passed" in r.stdout
    assert "demo-python -m ntx.ops.deploy_window" in calls, calls


def test_inside_the_window_the_check_refuses_before_the_stop(tmp_path):
    r, _ = _check(tmp_path, FAKE_WINDOW_RC="1")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "checks passed" not in r.stdout
    assert "bar" in r.stdout


def test_both_sides_of_the_close_are_asked(tmp_path):
    _, calls = _check(tmp_path)
    line = next(ln for ln in calls.splitlines() if "deploy_window" in ln)
    assert "--before" in line and "--quiet" in line, line


def test_a_broken_window_check_is_a_refusal_not_a_door(tmp_path):
    r, _ = _check(tmp_path, FAKE_WINDOW_RC="3")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "checks passed" not in r.stdout


def test_the_window_is_asked_after_the_confirmation():
    text = SCRIPT.read_text()
    check = text.split('if [ "$PHASE" = check ]')[1].split('if [ "$PHASE" = sync ]')[0]
    assert "deploy_window" in check
    assert check.index("read -r -p") < check.index("deploy_window") < check.index("checks passed")


# ---- the alerter must know a LIVE deploy is stopping the node (2026-10-11) ---- #


def test_a_passed_check_marks_a_live_deploy_in_progress(tmp_path):
    """2026-10-11 13:45: a live deploy stopped the node for ~7 min and the alerter
    pushed CRITICAL 'S003-live: node down or frozen?'. The marker the alerter reads
    was written only on the demo path, and without a role."""
    (tmp_path / "data").mkdir()
    r, _ = _check(tmp_path, FAKE_WINDOW_RC="0")
    assert r.returncode == 0, r.stdout + r.stderr
    marker = (tmp_path / "data" / ".deploy-in-progress").read_text().split()
    assert marker[1] == "live" and int(marker[0]) > 0


def test_a_refused_check_leaves_no_marker(tmp_path):
    (tmp_path / "data").mkdir()
    r, _ = _check(tmp_path, FAKE_WINDOW_RC="1")
    assert r.returncode == 2
    assert not (tmp_path / "data" / ".deploy-in-progress").exists()


def test_the_demo_deploy_marks_its_role():
    """O3-07: a role-less marker let a demo deploy excuse a live node going quiet."""
    src = (ROOT / "deploy" / "update.sh").read_text()
    line = next(x for x in src.splitlines() if x.startswith("mark_maintenance()"))
    assert "$ROLE" in line, line
