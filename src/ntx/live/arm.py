"""The live safety catch: real orders need a deliberate, separate arming step.

Both keys live in the server `.env` -- the demo one and the live one -- because
pasting a live key under pressure at go-live is its own risk. The cost of that
convenience is that `NTX_VENUE=okx_live`, one word in one file, would otherwise
be the only thing between a test run and real money.

So the two decisions are separated:

* `NTX_VENUE` picks the **account** (which keys, which host, which journal);
* this arm file decides whether the node may **send orders at all**.

    python -m ntx.live.arm --status
    sudo .venv/bin/python -m ntx.live.arm --on --note "go-live, S003, after the 7-day soak"   # root-owned file: L2-01
    python -m ntx.live.arm --off

Arming is typed by a human (`ARM LIVE`), recorded with a reason, and survives
nothing: it is a file under `data/`, so a fresh machine or a wiped data
directory starts disarmed. `--observe` and `--dry-run` never need it (neither
can order), and `ntx.ops.flatten --live` never needs it either -- the kill
switch must work when everything else is off.
"""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

ARM_FILENAME = "LIVE_ARMED"
CONFIRM = "ARM LIVE"
# The arm file lives OUTSIDE the repo checkout, in a root-owned directory: a
# push to GitHub runs as the bot user, and that user must not be able to arm
# real-money trading by writing a file into its own working tree (L2-01).
# `NTX_ARM_FILE` overrides it for tests and for a machine without /etc/ntx.
SYSTEM_ARM_FILE = Path("/etc/ntx") / ARM_FILENAME


class NotArmed(RuntimeError):
    """Live trading was requested on a node that nobody armed."""


def arm_path(data_dir: Path | str) -> Path:
    """Where the arming file lives.

    `NTX_ARM_FILE` wins, then the root-owned `/etc/ntx/LIVE_ARMED` when that
    directory exists, and only otherwise the data directory -- which is what
    local development and the tests use.
    """
    override = os.environ.get("NTX_ARM_FILE")
    if override:
        return Path(override)
    if SYSTEM_ARM_FILE.parent.is_dir():
        return SYSTEM_ARM_FILE
    return Path(data_dir) / ARM_FILENAME


def is_armed(data_dir: Path | str) -> bool:
    # lexists: a dangling symlink is still something someone planted.
    return os.path.lexists(arm_path(data_dir))


def arm(data_dir: Path | str, *, note: str = "") -> Path:
    path = arm_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    who = os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown"
    # Root runs this. A symlink planted at the path by the bot user would turn
    # that into a root write to a file of the bot user's choosing (L3-02), so
    # remove whatever is there and create the file fresh, never following a link.
    if path.is_symlink():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"armed_by={who}\nnote={note}\n")
    path.chmod(0o600)
    return path


def disarm(data_dir: Path | str) -> None:
    arm_path(data_dir).unlink(missing_ok=True)


def _owner_uid(path: Path) -> int:
    # lstat: never the owner of whatever a link points at (L3-02).
    return path.lstat().st_uid


def require_armed(settings, *, observe: bool = False, dry_run: bool = False) -> None:
    """Raise unless this run is allowed to send real orders.

    For LIVE the arm file must be **owned by root**, wherever it lives. A push to
    GitHub runs as the bot user; if that user could arm real-money trading by
    writing a file, a compromised commit could too (L2-01). A root-owned file can
    only come from a human with the admin password:

        sudo /home/ntx/ntx/.venv/bin/python -m ntx.live.arm --on --note "..."

    `NTX_ARM_ALLOW_ANY_OWNER=1` relaxes this for tests and local development.
    """
    if settings.is_demo or observe or dry_run:
        return
    path = arm_path(settings.data_dir)
    if os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode):
        # `ln -s /etc/hostname data/LIVE_ARMED` passed a stat()-based uid-0 check
        # as the bot user (L3-02): only a regular file, owned by root, arms.
        raise NotArmed(
            f"{path} is a symlink or not a regular file -- refusing it as an arm file. "
            f"Remove it and re-arm as root:\n"
            f"  sudo /home/ntx/ntx/.venv/bin/python -m ntx.live.arm --on --note '<why>'"
        )
    if path.exists():
        if os.environ.get("NTX_ARM_ALLOW_ANY_OWNER") == "1" or _owner_uid(path) == 0:
            return
        raise NotArmed(
            f"{path} exists but is not owned by root, so the bot's own user could have "
            f"written it -- which means a pushed commit could have armed real money "
            f"(L2-01). Re-arm as root:\n"
            f"  sudo /home/ntx/ntx/.venv/bin/python -m ntx.live.arm --on --note '<why>'"
        )
    raise NotArmed(
        f"LIVE trading is not armed. {arm_path(settings.data_dir)} does not exist.\n"
        f"  check the key safely : python -m ntx.live.node --strategy S003 --observe\n"
        f"  arm for real money   : sudo .venv/bin/python -m ntx.live.arm --on --note '<why>'\n"
        f"  (the kill switch `python -m ntx.ops.flatten --live` works either way)"
    )


def main(argv: list[str] | None = None) -> int:
    from ntx.config.settings import load

    ap = argparse.ArgumentParser(description="arm / disarm real-money trading")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true", help=f"allow real orders (types {CONFIRM!r})")
    g.add_argument("--off", action="store_true", help="back to safe: no real orders")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--note", default="", help="why, for the record")
    args = ap.parse_args(argv)
    settings = load()
    data_dir = settings.data_dir

    if args.status:
        state = "ARMED -- real orders allowed" if is_armed(data_dir) else "disarmed (safe)"
        print(f"{state}  [{arm_path(data_dir)}]")
        if is_armed(data_dir):
            try:
                print(arm_path(data_dir).read_text().strip())
            except PermissionError:
                # /etc/ntx/LIVE_ARMED is 0600 root: the bot user can see that it
                # exists (which is all arming needs) but not read the note.
                print("(note readable by root only -- run with sudo to see it)")
        return 0

    if args.off:
        disarm(data_dir)
        print("disarmed: the live node will refuse to start")
        return 0

    if not args.note:
        print("--note is required: say why this is being armed", flush=True)
        return 2
    typed = input(f"This allows REAL orders on the live OKX account. Type {CONFIRM} to confirm: ")
    if typed.strip() != CONFIRM:
        print("not confirmed -- still disarmed")
        return 2
    path = arm(data_dir, note=args.note)
    print(f"ARMED: {path}\nDisarm with: python -m ntx.live.arm --off")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
