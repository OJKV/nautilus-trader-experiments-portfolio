"""One node per strategy per account (F046).

Two nodes with the same trader id on one OKX account fight: the second adopts
what the first owns, OKX refuses the duplicate stop ("You can only place 1
TP/SL order"), and the guard flattens. The venue cannot tell them apart, so the
box has to.

    with single_instance(plan.trader_id, settings.data_dir, demo=plan.demo):
        node.run()

Two properties the first version lacked (review 2026-09-18):

* **The demo soak and the live node are different accounts** and must not share
  a lock, or whichever starts second ends `failed` -- after a reboot possibly
  the live one (L-04). The environment is part of the lock name.
* **The kernel owns the lock, not a pid written in a file** (L-03). `flock` is
  released by the kernel however the holder dies, so a stale pid from a hard
  reboot can never wedge the unit; and there is no window between checking and
  writing. The pid is still written, for humans reading the file.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    """Another node holds the lock for this trader id."""


def lock_path(trader_id: str, data_dir: Path, *, demo: bool) -> Path:
    return Path(data_dir) / "locks" / f"{trader_id}-{'demo' if demo else 'live'}.lock"


@contextmanager
def single_instance(trader_id: str, data_dir: Path, *, demo: bool = False):
    """Hold an exclusive lock for this trader id + environment."""
    path = lock_path(trader_id, data_dir, demo=demo)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno not in (errno.EACCES, errno.EAGAIN):
            os.close(fd)
            raise
        try:
            holder = os.read(fd, 64).decode().strip() or "?"
        except OSError:
            holder = "?"
        os.close(fd)
        raise AlreadyRunning(
            f"{trader_id} ({'demo' if demo else 'live'}) is already running as pid {holder} "
            f"({path}). Stop it first -- two nodes on one account fight over the same position."
        ) from None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        yield path
    finally:
        # The kernel releases the lock when the fd closes, whatever killed us.
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
        os.close(fd)
