"""Is the node that wrote this snapshot actually healthy?

`systemctl restart` returning 0 means the process started, not that the bot
works (R31: verify the read-back). This reads the state snapshot the node
rewrites every 15 s and answers the only question a deploy cares about: is it
up, connected, warm and protected?

    python -m ntx.ops.snapshot_check data/state/S003-demo.json --max-age 120

Exit 0 healthy · 1 unhealthy (reasons on stdout) · 2 no snapshot at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def problems(
    snap: dict, *, now: float, max_age_s: float, expect_demo: bool | None = None
) -> list[str]:
    """Everything wrong with this snapshot, in plain words."""
    out: list[str] = []
    age = abs(now - float(snap.get("ts_ns", 0)) / 1e9)
    if age > max_age_s:
        out.append(f"snapshot is {age:.0f}s old (limit {max_age_s:.0f}s) -- node down or frozen")
    if int(snap.get("positions_unprotected") or 0):
        out.append(f"{snap['positions_unprotected']} position(s) without a venue stop (R15)")
    for p in snap.get("positions") or []:
        if p.get("protected") is False:
            out.append(f"{p.get('instrument', '?')} has no live stop")
    if expect_demo is not None and bool(snap.get("is_demo")) is not expect_demo:
        out.append(
            f"is_demo={snap.get('is_demo')} but this unit expects "
            f"{'demo' if expect_demo else 'live'} -- wrong account"
        )
    if snap.get("trading_enabled") is False:
        out.append("trading is disabled (observe mode?)")
    held, sized = snap.get("account_equity_usd"), snap.get("sizing_equity_usd")
    if held is not None and sized is not None and float(sized) > float(held):
        # An OKX demo account can reset itself mid-soak (F059): the node keeps
        # sizing from the override and every order is refused by the venue.
        out.append(
            f"the account holds ${float(held):,.2f} but trades are sized as if "
            f"${float(sized):,.2f} -- orders will be refused for insufficient balance"
        )
    if snap.get("killed"):
        # A KILL-latched bot is up, connected and writing a fresh snapshot while
        # refusing to trade. Without this it reads green everywhere (O2-07).
        out.append(
            "KILL is latched: the bot is halted and will not enter -- run ntx.ops.unkill --clear --bot <bot>"
        )
    not_ready = [
        i.get("id", "?")
        for i in snap.get("instruments") or []
        # `ready` is absent on snapshots written before 2026-09-19; `warm` is the
        # older, weaker signal and is accepted as a fallback rather than failing.
        if not (i.get("ready") if i.get("ready") is not None else i.get("warm"))
    ]
    if not_ready:
        out.append(f"not ready to signal: {', '.join(not_ready)}")
    if not (snap.get("instruments") or []):
        out.append("no instruments in the snapshot")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="health-check a node state snapshot")
    ap.add_argument("path")
    ap.add_argument("--max-age", type=float, default=120.0)
    ap.add_argument("--demo", action="store_true", help="require is_demo=true")
    ap.add_argument("--live", action="store_true", help="require is_demo=false")
    args = ap.parse_args(argv)
    path = Path(args.path)
    try:
        snap = json.loads(path.read_text())
    except FileNotFoundError:
        print(f"no snapshot at {path}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"snapshot unreadable: {e}", file=sys.stderr)
        return 2
    expect = True if args.demo else (False if args.live else None)
    found = problems(snap, now=time.time(), max_age_s=args.max_age, expect_demo=expect)
    if found:
        for p in found:
            print(f"UNHEALTHY: {p}")
        return 1
    print(f"healthy: {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
