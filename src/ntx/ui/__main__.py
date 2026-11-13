"""Run the NTX Monitor: `python -m ntx.ui [--port 8787]`.

Binds 127.0.0.1 only -- reach it over an SSH tunnel:
    ssh -L 8787:127.0.0.1:8787 ntx@<server>   then open http://localhost:8787

Does not load Settings/.env: the monitor never needs (or holds) an API key.
Paths come from flags, then NTX_DATA_DIR, then the repo layout (all absolute, R21).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from ntx.ui.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
ALLOWED_HOST = "127.0.0.1"


def _abs(p: str | os.PathLike, base: Path = REPO_ROOT) -> Path:
    path = Path(p).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ntx.ui", description="NTX Monitor (read-only)")
    ap.add_argument("--host", default=ALLOWED_HOST, help="must be 127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--data-dir", default=os.environ.get("NTX_DATA_DIR") or REPO_ROOT / "data")
    ap.add_argument("--logs-dir", default=REPO_ROOT / "logs")
    ap.add_argument("--reports-dir", default=REPO_ROOT / "reports")
    args = ap.parse_args(argv)

    if args.host != ALLOWED_HOST:
        print(
            f"refusing to bind {args.host!r}: the monitor listens on {ALLOWED_HOST} only; "
            "use an SSH tunnel (ssh -L 8787:127.0.0.1:8787 ...)",
            file=sys.stderr,
        )
        raise SystemExit(2)

    data, logs, reports = _abs(args.data_dir), _abs(args.logs_dir), _abs(args.reports_dir)
    print(f"NTX Monitor on http://{ALLOWED_HOST}:{args.port}  data={data}  logs={logs}")
    uvicorn.run(
        create_app(data, logs, reports),
        host=ALLOWED_HOST,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
