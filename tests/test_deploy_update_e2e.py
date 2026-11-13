"""`deploy/update.sh` run end to end against a scratch origin and a fake box.

The other deploy tests pin the script's shape; these RUN it. A broken update.sh
breaks every later deploy (ARGUS F-083), and merging a change to it deploys it
to the demo box within a minute -- so the paths that stop, roll back and skip
are exercised here for real, with git, and with fakes only for what would touch
the machine: `systemctl`, `sudo`, `uv` and the checkout's python.

R-007 findings reproduced here (each test names its own):
  L3-07  a red commit bounced both demo bots on every timer tick
  L3-09  a demo bot in `activating (auto-restart)` was not stopped before the
         merge and `uv sync` ran under it
  L2-09  a crashed `deploy_window` read as "wait" -- deploys stopped forever, silently
  L2-11  `wait_healthy` accepted a snapshot written BEFORE the stop
  new    a docs-only push stopped both bots and ran the full suite
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = (ROOT / "deploy" / "update.sh").read_text()
UNITS = ("ntx-soak.service", "ntx-s004-demo2.service")

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
S="$FAKE_UNITS"
case "$1" in
  is-active) shift; [ "$1" = --quiet ] && shift
             [ "$(cat "$S/$1" 2>/dev/null)" = active ] ;;
  show)      u="${@: -1}"; cat "$S/$u" 2>/dev/null || echo inactive ;;
  stop)      echo inactive > "$S/$2" ;;
  start)     echo active > "$S/$2"
             if [ -z "${FAKE_NO_SNAPSHOT:-}" ]; then
               sleep 1   # bash 3.2's -nt has one-second resolution
               case "$2" in
                 ntx-soak.service) touch "$FAKE_DATA/state/S003-demo.json" ;;
                 ntx-s004-demo2.service) touch "$FAKE_DATA/state/S004-demo.json" ;;
               esac
             fi ;;
  reset-failed) ;;
  *) exit 1 ;;
esac
"""

FAKE_SUDO = r"""#!/usr/bin/env bash
[ "$1" = -n ] && shift
if [ "$1" = -l ]; then
  echo "(root) NOPASSWD: /usr/bin/systemctl stop ntx-soak.service, /usr/bin/systemctl stop ntx-s004-demo2.service"
  exit 0
fi
exec "$@"
"""

FAKE_UV = r"""#!/usr/bin/env bash
echo "uv $*" >> "$FAKE_LOG"
case "$1" in
  sync) exit 0 ;;
  run)  if [ -e RED ]; then echo "FAILED tests/test_red.py::test_red - boom"; exit 1; fi
        echo "3 passed"; exit 0 ;;
esac
"""

# The checkout's own interpreter. deploy_window answers from FAKE_WINDOW_RC;
# snapshot_check is "a file under 90 s old", which is what the real one checks.
FAKE_PYTHON = r"""#!/usr/bin/env bash
echo "python $*" >> "$FAKE_LOG"
case "$*" in
  *ntx.ops.deploy_window*) exit "${FAKE_WINDOW_RC:-0}" ;;
  *ntx.ops.snapshot_check*)
    f="$3"
    [ -f "$f" ] && /usr/bin/perl -e 'exit((time - (stat shift)[9]) < 90 ? 0 : 1)' "$f" ;;
  *) exit 0 ;;
esac
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _exe(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class Box:
    """A scratch origin, a demo checkout cloned from it, and fakes for the rest."""

    def __init__(self, tmp: Path, script: str = UPDATE_SH):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.seed = tmp / "seed"
        self.demo = tmp / "demo"
        self.data = tmp / "data"
        self.units = tmp / "units"
        self.log = tmp / "calls.log"
        self.bin = tmp / "bin"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.seed.mkdir()
        _git(self.seed, "init", "-q", "-b", "main")
        _git(self.seed, "config", "user.email", "t@example.invalid")
        _git(self.seed, "config", "user.name", "t")
        _exe(self.seed / "deploy" / "update.sh", script)
        (self.seed / ".gitignore").write_text("/.venv/\n/data/\n")
        (self.seed / "src").mkdir()
        (self.seed / "src" / "x.py").write_text("x = 0\n")
        (self.seed / "docs").mkdir()
        (self.seed / "docs" / "a.md").write_text("# a\n")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-qm", "c0")
        _git(self.seed, "remote", "add", "origin", str(self.origin))
        _git(self.seed, "push", "-q", "origin", "main")
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.demo)],
            check=True,
            capture_output=True,
        )
        _exe(self.demo / ".venv" / "bin" / "python", FAKE_PYTHON)
        _exe(self.bin / "systemctl", FAKE_SYSTEMCTL)
        _exe(self.bin / "sudo", FAKE_SUDO)
        _exe(self.bin / "uv", FAKE_UV)
        (self.data / "state").mkdir(parents=True)
        self.units.mkdir()
        for u in UNITS:
            (self.units / u).write_text("active\n")
        # The bots were running and their environment was verified at c0.
        for snap in ("S003-demo.json", "S004-demo.json"):
            (self.data / "state" / snap).write_text("{}")
        (self.data / ".deploy-verified").write_text(f"{self.head()} {int(time.time())}\n")

    def head(self) -> str:
        return _git(self.demo, "rev-parse", "HEAD")

    def push(self, files: dict[str, str | None], msg: str = "change") -> str:
        for name, body in files.items():
            p = self.seed / name
            if body is None:
                p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body)
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-qm", msg)
        _git(self.seed, "push", "-q", "origin", "main")
        return _git(self.seed, "rev-parse", "HEAD")

    def tick(self, **env: str) -> subprocess.CompletedProcess:
        self.log.write_text("")
        return subprocess.run(
            ["bash", str(self.demo / "deploy" / "update.sh")],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "HOME": str(self.tmp),
                "NTX_DEMO_REPO": str(self.demo),
                "NTX_LIVE_REPO": str(self.tmp / "no-live-checkout"),
                "NTX_DATA_DIR": str(self.data),
                "FAKE_LOG": str(self.log),
                "FAKE_UNITS": str(self.units),
                "FAKE_DATA": str(self.data),
                "NTX_DEPLOY_HEALTHY_S": "6",
                **env,
            },
        )

    def calls(self) -> str:
        return self.log.read_text()

    def stops(self) -> list[str]:
        return [ln for ln in self.calls().splitlines() if ln.startswith("systemctl stop")]

    def state(self, unit: str) -> str:
        return (self.units / unit).read_text().strip()


def _out(r: subprocess.CompletedProcess) -> str:
    return f"rc={r.returncode}\n--- stdout\n{r.stdout}\n--- stderr\n{r.stderr}"


# ---- the ordinary path, which every fix below must leave working ----------- #


def test_a_code_commit_stops_tests_and_restarts_both_bots(tmp_path):
    box = Box(tmp_path)
    new = box.push({"src/x.py": "x = 1\n"})
    r = box.tick()
    assert r.returncode == 0, _out(r)
    assert box.head() == new
    assert sorted(box.stops()) == sorted(f"systemctl stop {u}" for u in UNITS), box.calls()
    assert "uv run --frozen pytest -q" in box.calls()
    assert all(box.state(u) == "active" for u in UNITS)
    assert (box.data / ".deploy-verified").read_text().split()[0] == new


def test_a_red_commit_is_rolled_back_and_the_bots_come_back_on_the_old_code(tmp_path):
    box = Box(tmp_path)
    old = box.head()
    box.push({"RED": "x\n"}, "red")
    r = box.tick()
    assert r.returncode == 1, _out(r)
    assert box.head() == old, "the rollback did not reset the checkout"
    assert all(box.state(u) == "active" for u in UNITS), "a bot was left down"
    assert "deploy failed and rolled back" in box.calls()
    assert "ROLLBACK INCOMPLETE" not in box.calls()


# ---- L3-07: a red commit must not bounce the bots on every tick ------------ #


def test_l3_07_the_same_red_commit_is_not_retried_every_tick(tmp_path):
    box = Box(tmp_path)
    box.push({"RED": "x\n"}, "red")
    first = box.tick()
    assert first.returncode == 1, _out(first)

    second = box.tick()
    assert second.returncode == 0, _out(second)
    assert box.stops() == [], (
        "the bots were stopped again for a commit that already failed here:\n" + box.calls()
    )
    assert "pytest" not in box.calls()
    assert "already failed" in second.stdout, second.stdout


def test_l3_07_a_new_commit_after_a_red_one_is_deployed(tmp_path):
    """The stamp skips one SHA, not the pipeline: a fix on top deploys."""
    box = Box(tmp_path)
    box.push({"RED": "x\n"}, "red")
    assert box.tick().returncode == 1
    # (a fix that only deleted RED would leave a tree identical to the verified
    # one, which correctly takes the no-restart path; change code too)
    fixed = box.push({"RED": None, "src/x.py": "x = 3\n"}, "fix")
    r = box.tick()
    assert r.returncode == 0, _out(r)
    assert box.head() == fixed
    assert len(box.stops()) == 2


def test_l3_07_the_failure_says_why(tmp_path):
    """L2-11: `pytest >/dev/null` left a rollback with no diagnostics at all."""
    box = Box(tmp_path)
    box.push({"RED": "x\n"}, "red")
    r = box.tick()
    assert "test_red" in r.stdout, r.stdout


# ---- L3-09: a bot in auto-restart is still a running bot ------------------- #


def test_l3_09_a_bot_in_auto_restart_is_stopped_before_the_merge(tmp_path):
    box = Box(tmp_path)
    (box.units / "ntx-s004-demo2.service").write_text("activating\n")
    box.push({"src/x.py": "x = 1\n"})
    r = box.tick()
    assert r.returncode == 0, _out(r)
    assert "systemctl stop ntx-s004-demo2.service" in box.stops(), box.calls()
    calls = box.calls()
    assert calls.index("systemctl stop ntx-s004-demo2.service") < calls.index("uv sync")
    assert box.state("ntx-s004-demo2.service") == "active", "it must be started again after"


def test_l3_09_a_stopped_bot_is_left_stopped(tmp_path):
    """The positive control: `inactive` and `failed` are not running."""
    box = Box(tmp_path)
    (box.units / "ntx-s004-demo2.service").write_text("failed\n")
    box.push({"src/x.py": "x = 1\n"})
    r = box.tick()
    assert r.returncode == 0, _out(r)
    assert box.stops() == ["systemctl stop ntx-soak.service"], box.calls()
    assert box.state("ntx-s004-demo2.service") == "failed"


# ---- docs-only pushes do not touch the bots ------------------------------- #


def test_a_docs_only_push_advances_the_checkout_without_stopping_anything(tmp_path):
    box = Box(tmp_path)
    new = box.push({"docs/a.md": "# a, revised\n", "docs/b.md": "# b\n", "README.md": "hi\n"})
    r = box.tick()
    assert r.returncode == 0, _out(r)
    assert box.head() == new, "the docs were not fast-forwarded"
    assert box.stops() == [], box.calls()
    assert "systemctl start" not in box.calls()
    assert "uv " not in box.calls(), "no sync and no suite for a docs-only change"
    assert "deploy_window" not in box.calls(), "nothing restarts, so no bar window to wait for"
    assert (box.data / ".deploy-verified").read_text().split()[0] == new
    assert "docs-only" in r.stdout


def test_a_docs_only_push_keeps_the_re_verify_clock(tmp_path):
    """Carrying the stamp forward must not reset the 6-hourly re-verify."""
    box = Box(tmp_path)
    old_ts = int(time.time()) - 3600
    (box.data / ".deploy-verified").write_text(f"{box.head()} {old_ts}\n")
    box.push({"docs/a.md": "# a2\n"})
    assert box.tick().returncode == 0
    assert int((box.data / ".deploy-verified").read_text().split()[1]) == old_ts


def test_a_push_touching_code_and_docs_takes_the_full_path(tmp_path):
    box = Box(tmp_path)
    box.push({"docs/a.md": "# a2\n", "src/x.py": "x = 2\n"})
    r = box.tick()
    assert r.returncode == 0, _out(r)
    assert len(box.stops()) == 2, box.calls()
    assert "pytest" in box.calls()


def test_a_markdown_file_outside_docs_that_is_not_md_is_code(tmp_path):
    """Allowlist, not denylist: a non-.md file under docs/ is still docs, but
    anything else -- a .txt at the root, a lock file -- takes the full path."""
    box = Box(tmp_path)
    box.push({"uv.lock": "x\n"})
    assert box.tick().returncode == 0
    assert len(box.stops()) == 2, box.calls()


def test_a_docs_only_push_on_an_unverified_checkout_takes_the_full_path(tmp_path):
    """The shortcut is "same code as a verified commit". With no fresh stamp for
    the old commit there is nothing to carry forward."""
    box = Box(tmp_path)
    (box.data / ".deploy-verified").unlink()
    box.push({"docs/a.md": "# a2\n"})
    r = box.tick()
    assert r.returncode == 0, _out(r)
    assert "pytest" in box.calls(), box.calls()


# ---- L2-09: a broken window check must be loud, not a silent "wait" -------- #


def test_l2_09_inside_the_bar_window_the_deploy_waits_quietly(tmp_path):
    box = Box(tmp_path)
    old = box.head()
    box.push({"src/x.py": "x = 1\n"})
    r = box.tick(FAKE_WINDOW_RC="1")
    assert r.returncode == 0, _out(r)
    assert box.stops() == [] and box.head() == old
    assert "ntx.obs.alerts" not in box.calls()


def test_l2_09_a_crashed_window_check_alerts_and_does_not_deploy(tmp_path):
    box = Box(tmp_path)
    old = box.head()
    box.push({"src/x.py": "x = 1\n"})
    r = box.tick(FAKE_WINDOW_RC="2")
    assert box.stops() == [] and box.head() == old
    assert "ntx.obs.alerts" in box.calls(), "a broken safety check must not be silent"
    assert "REFUSING" in r.stdout, r.stdout
    # exit 0: the push is deduplicated; the OnFailure crash hook is not and
    # would page every minute
    assert r.returncode == 0, _out(r)


def test_l2_09_the_window_is_guarded_on_both_sides_of_the_bar(tmp_path):
    box = Box(tmp_path)
    box.push({"src/x.py": "x = 1\n"})
    box.tick()
    line = next(ln for ln in box.calls().splitlines() if "deploy_window" in ln)
    assert "--before" in line, line


# ---- L2-11: health means a snapshot written AFTER the start ---------------- #


def test_l2_11_a_snapshot_from_before_the_stop_is_not_health(tmp_path):
    """The bots wrote a snapshot seconds before the deploy stopped them. If the
    new code never writes one, that old file must not read as "healthy"."""
    box = Box(tmp_path)
    box.push({"src/x.py": "x = 1\n"})
    r = box.tick(FAKE_NO_SNAPSHOT="1")
    assert r.returncode == 1, _out(r)
    assert "deployed" not in r.stdout.splitlines()[-1], r.stdout
    assert "ROLLBACK INCOMPLETE" in box.calls() or "rolled back" in box.calls()


# ---- the deploy that ships a new update.sh -------------------------------- #


def test_a_red_commit_that_changes_update_sh_rolls_back_through_the_re_exec(tmp_path):
    """The new script runs after the merge (F-083 re-exec). Its rollback must
    know which commit failed, or the stamp records nothing and the loop is back."""
    box = Box(tmp_path)
    old = box.head()
    new = box.push({"deploy/update.sh": UPDATE_SH + "# touched\n", "RED": "x\n"}, "red+script")
    r = box.tick()
    assert r.returncode == 1, _out(r)
    assert "running the updated deploy script" in r.stdout, r.stdout
    assert box.head() == old
    assert all(box.state(u) == "active" for u in UNITS)
    assert (box.data / ".deploy-failed").read_text().split()[0] == new
    assert box.tick().returncode == 0 and box.stops() == [], box.calls()


def _main_update_sh() -> str | None:
    r = subprocess.run(
        ["git", "show", "main:deploy/update.sh"], cwd=ROOT, capture_output=True, text=True
    )
    return r.stdout if r.returncode == 0 and r.stdout.startswith("#!") else None


def test_the_running_script_on_main_can_deploy_this_one(tmp_path):
    """Merging a change to update.sh is executed by the PREVIOUS update.sh, which
    then re-execs the new one with only the environment it knows to pass. A new
    script that needs more than that breaks every later deploy."""
    import pytest

    previous = _main_update_sh()
    if previous is None:
        pytest.skip("no local main branch to take the previous update.sh from")
    box = Box(tmp_path, script=previous)
    new = box.push({"deploy/update.sh": UPDATE_SH, "src/x.py": "x = 9\n"}, "ship")
    r = box.tick()
    assert r.returncode == 0, _out(r)
    assert box.head() == new
    assert all(box.state(u) == "active" for u in UNITS)
    # and the next red commit is handled by the new script
    box.push({"RED": "x\n"}, "red")
    assert box.tick().returncode == 1
    assert box.tick().returncode == 0 and box.stops() == [], box.calls()
