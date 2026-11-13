"""Suite-wide setup.

A git hook (the global test-gate pre-commit) runs this suite with GIT_DIR and
GIT_INDEX_FILE exported. Tests that build throwaway checkouts in a temp dir
then ran `git init` / `git config` / `git remote add` against the REAL
repository and rewrote its shared config (tests/test_hook_env_isolation.py).
Drop them before anything runs: no test may act on the repo it is testing.
"""

import os

for _name in (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
):
    os.environ.pop(_name, None)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_system_arm_file(monkeypatch, tmp_path_factory):
    """Never let a test see the machine's /etc/ntx/LIVE_ARMED.

    `arm_path` prefers /etc/ntx whenever it exists. On the VPS it does (since
    2026-10-07), so tests that arm a tmp dir read the real root-only file and
    failed -- which rolled a live deploy back. Point it at a dir that does not exist.
    """
    import ntx.live.arm as arm

    missing = tmp_path_factory.mktemp("no-etc-ntx") / "absent" / arm.ARM_FILENAME
    monkeypatch.setattr(arm, "SYSTEM_ARM_FILE", missing)
