"""Atomic file writes.

Failure pattern 3 (docs/13-production-hardening.md §1): a JSON state file written
directly is truncated if the process dies mid-flush. The next reader gets invalid
JSON and, in the reported case, retried forever -- the bot never traded again.

Contract:
  write: serialise -> temp file in the SAME directory -> parse-validate ->
         os.replace() (atomic on POSIX)
  read:  on corruption, DELETE the file and return None. Never retry a broken
         state indefinitely.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["atomic_write_json", "atomic_write_text", "safe_read_json"]


def atomic_write_text(path: Path, text: str, *, validate: bool = False) -> None:
    """Write `text` to `path` atomically.

    The temp file is created in the target's directory so `os.replace` stays on
    one filesystem (a cross-device rename is not atomic).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # durability before the rename, not after
        if validate:
            json.loads(tmp.read_text(encoding="utf-8"))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    """Serialise `data` as JSON and write it atomically, validating before commit."""
    atomic_write_text(Path(path), json.dumps(data, indent=2, default=str), validate=True)


def safe_read_json(path: Path, *, delete_corrupt: bool = True) -> Any | None:
    """Read JSON, tolerating absence and corruption.

    Returns None if the file is missing or unparseable. A corrupt file is deleted
    by default so a stuck state cannot block the pipeline forever.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        if delete_corrupt:
            path.unlink(missing_ok=True)
        return None
