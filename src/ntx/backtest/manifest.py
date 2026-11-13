"""Run lineage: every backtest result must be reproducible from disk alone.

R9 requires config + code version + data version with every result. This module
enforces it by writing a self-contained directory per run:

    reports/runs/<timestamp>-<label>-<hash>/
        manifest.json    what was run, with what code, against what data
        metrics.json     the computed metrics
        trades.csv       every closed position
        equity.csv       the mark-to-market equity curve
        funding.csv      per-trade funding, when applied
        stdout.txt       whatever the caller printed

Why a **source hash** and not only a git SHA: this repo currently has no commits,
so no SHA exists. Hashing the contents of `src/ntx/**/*.py` gives a version
identifier that works regardless, and keeps working if someone runs with
uncommitted edits -- which is exactly when a git SHA lies.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_DIR = REPO_ROOT / "reports" / "runs"


def _sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, cwd=REPO_ROOT, check=False
        ).stdout.strip()
    except Exception:
        return ""


def source_hash() -> dict[str, Any]:
    """SHA256 over every tracked source file's contents, plus per-file digests.

    Per-file digests are included so a reviewer can see exactly which module
    differs between two runs rather than only that something did.
    """
    files = sorted((REPO_ROOT / "src" / "ntx").rglob("*.py"))
    per_file = {}
    combined = hashlib.sha256()
    for f in files:
        data = f.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        rel = str(f.relative_to(REPO_ROOT))
        per_file[rel] = digest[:12]
        combined.update(rel.encode())
        combined.update(digest.encode())
    return {"combined": combined.hexdigest()[:16], "files": per_file}


def data_fingerprint(data_dir: Path) -> dict[str, Any]:
    """Identify the dataset by its catalog contents, not by a mutable path."""
    catalog = Path(data_dir) / "catalog"
    if not catalog.exists():
        return {"present": False}
    parts = sorted(catalog.rglob("*.parquet"))
    h = hashlib.sha256()
    total = 0
    for p in parts:
        total += p.stat().st_size
        h.update(str(p.relative_to(catalog)).encode())
        # CONTENT, not just name+size: a same-size edit used to produce the same
        # fingerprint, so a changed dataset could pass as the same version (M-02).
        with p.open("rb") as fh:
            for chunk in iter(lambda fh=fh: fh.read(1024 * 1024), b""):
                h.update(chunk)
    return {
        "present": True,
        "files": len(parts),
        "bytes": total,
        "hash": h.hexdigest()[:16],
        "bar_types": sorted(p.name for p in (catalog / "data" / "bar").iterdir())
        if (catalog / "data" / "bar").exists()
        else [],
    }


def environment() -> dict[str, Any]:
    import nautilus_trader

    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "nautilus_trader": nautilus_trader.__version__,
        "pandas": pd.__version__,
        # `git rev-parse HEAD` echoes the literal "HEAD" when the repo has no
        # commits, so validate the shape rather than trusting the string.
        "git_sha": (
            sha
            if len(sha := _sh(["git", "rev-parse", "HEAD"])) == 40
            and all(c in "0123456789abcdef" for c in sha)
            else "NO_COMMITS"
        ),
        "git_dirty": bool(_sh(["git", "status", "--porcelain"])),
    }


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, type):
        return f"{obj.__module__}.{obj.__name__}"
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def record(
    *,
    label: str,
    spec: Any,
    metrics: Any = None,
    positions: pd.DataFrame | None = None,
    equity: pd.Series | None = None,
    funding: pd.Series | None = None,
    extra: dict[str, Any] | None = None,
    data_dir: Path | None = None,
    stdout: str = "",
) -> Path:
    """Write a complete, self-describing run directory. Returns its path."""
    src = source_hash()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:48]
    out = RUNS_DIR / f"{stamp}-{safe}-{src['combined'][:8]}"
    out.mkdir(parents=True, exist_ok=True)

    manifest = {
        "label": label,
        "created_utc": datetime.now(UTC).isoformat(),
        "spec": _jsonable(spec),
        "environment": environment(),
        "source_hash": src,
        "data": data_fingerprint(data_dir or (REPO_ROOT / "data")),
        "extra": _jsonable(extra or {}),
        "reproduce": (
            "Re-run the script named in extra.script with the same source_hash "
            "and data.hash. Differing source_hash.files show which modules changed."
        ),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if metrics is not None:
        (out / "metrics.json").write_text(json.dumps(_jsonable(metrics), indent=2))
    if positions is not None and not positions.empty:
        positions.to_csv(out / "trades.csv")
    if equity is not None and len(equity):
        equity.to_csv(out / "equity.csv", header=True)
    if funding is not None and len(funding):
        funding.to_csv(out / "funding.csv", header=True)
    if stdout:
        (out / "stdout.txt").write_text(stdout)
    return out


def index() -> pd.DataFrame:
    """Every recorded run, newest first -- the review entry point."""
    rows = []
    if not RUNS_DIR.exists():
        return pd.DataFrame()
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        mf = d / "manifest.json"
        if not mf.exists():
            continue
        m = json.loads(mf.read_text())
        met = {}
        if (d / "metrics.json").exists():
            met = json.loads((d / "metrics.json").read_text())
        rows.append(
            {
                "run": d.name,
                "label": m.get("label"),
                "created": m.get("created_utc", "")[:19],
                "source": m.get("source_hash", {}).get("combined"),
                "data": m.get("data", {}).get("hash"),
                "nautilus": m.get("environment", {}).get("nautilus_trader"),
                "trades": met.get("trades"),
                "sharpe": met.get("sharpe"),
                "return_pct": met.get("return_pct"),
                "maxdd_pct": met.get("max_drawdown_pct"),
            }
        )
    return pd.DataFrame(rows)
