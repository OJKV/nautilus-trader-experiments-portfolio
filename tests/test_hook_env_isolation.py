"""The suite must never inherit a git hook's environment.

The global test-gate pre-commit hook runs this suite with git's GIT_DIR /
GIT_INDEX_FILE exported. `test_deploy` builds throwaway checkouts with
`git init` / `git config user.name t` / `git remote add` in a temp directory --
and with GIT_DIR inherited, those ran against the REAL repository: on
2026-10-07 the shared .git/config came out with `user.name = t` and
`core.worktree = <a pytest temp dir>`, so a commit was authored by "t" and the
hook then skipped the suite as "no test suite found".

conftest.py drops those variables before any test runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HOOK_ENV = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")


def test_no_git_hook_environment_reaches_the_tests():
    leaked = sorted(k for k in HOOK_ENV if k in os.environ)
    assert not leaked, f"git hook environment leaked into the suite: {leaked}"


def test_a_hook_run_does_not_leak_into_the_suite(tmp_path):
    """Run the check above the way the hook does: with GIT_DIR exported."""
    env = {**os.environ, "GIT_DIR": str(tmp_path / "not-a-repo"), "GIT_INDEX_FILE": "x"}
    here = Path(__file__)
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"{here}::test_no_git_hook_environment_reaches_the_tests",
        ],
        cwd=here.parent.parent,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout[-800:]
