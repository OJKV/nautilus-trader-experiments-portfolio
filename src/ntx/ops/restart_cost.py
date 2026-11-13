"""What a restart costs, stated in full, when the account is not flat.

The live deploy used to refuse outright over an open position. That is the right
default and the wrong only option: S003 holds a position most of the time -- the
account was measured flat just 31.5% of the time, with a worst-case holding
stretch of 28.5 days -- so "wait until flat" is not a wait, it is a deadlock. A
gate nobody can pass gets worked around, and the workaround is worse than the
thing the gate was protecting.

So the refusal becomes a disclosure. A restart does exactly one risky thing: it
cancels each position's venue stop and places it again, and between those two
calls the position has no stop. That window is the entire cost, it is measured
per fill in the journal (`protect_gap_ms`), and this prints it next to every
position the restart would touch.

    python -m ntx.ops.restart_cost --live

Exit 0 whether or not anything is open: this reports, it does not decide. The
decision is the operator's, and `deploy-live.sh` asks for it in a different
sentence from the ordinary one so it cannot be answered by muscle memory.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from ntx.config.geo import check_egress
from ntx.config.settings import load
from ntx.config.venues import OKX
from ntx.doctor import _signed_get

POSITIONS = "/api/v5/account/positions?instType=SWAP"
ALGOS = "/api/v5/trade/orders-algo-pending?ordType=conditional"


def protect_gaps(journal_path: Path) -> list[int]:
    """Every measured fill-to-stop gap, in milliseconds.

    Measured, never assumed (R11). An estimate printed above a real decision is
    worth less than nothing, because it looks like evidence.
    """
    try:
        con = sqlite3.connect(f"file:{Path(journal_path)}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT protect_gap_ms FROM trades WHERE protect_gap_ms IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return sorted(int(r[0]) for r in rows)


def gap_sentence(gaps: list[int]) -> str:
    if not gaps:
        return (
            "No re-protect time has been measured on this account yet, so the window "
            "below is unknown rather than small."
        )
    mid = gaps[len(gaps) // 2]
    return (
        f"Measured re-protect time on this account: median {mid} ms, "
        f"worst {gaps[-1]} ms, over {len(gaps)} recorded fill(s)."
    )


def report(positions: list[dict], stops: dict[str, str], gaps: list[int]) -> str:
    """The whole disclosure, as the operator will read it."""
    if not positions:
        return "The account is flat. A restart touches no position."
    lines = [
        f"{len(positions)} open position(s). A restart cancels each venue stop and "
        f"places it again; in between, that position has no stop:"
    ]
    for p in positions:
        inst = str(p.get("instId", "?"))
        stop = stops.get(inst)
        guard = f"stop {stop}" if stop else "NO VENUE STOP FOUND"
        lines.append(f"  {inst:<20} pos {p.get('pos', '?')!s:<10} {guard}")
    lines.append(gap_sentence(gaps))
    naked = [p for p in positions if not stops.get(str(p.get("instId", "")))]
    if naked:
        lines.append(
            "A position with no stop at the venue is already unprotected: restarting "
            "is not what puts it at risk, but do not deploy over one without knowing why."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="what a restart would cost right now")
    # Required: a flagless run used to follow .env, which on the box names the
    # OTHER account half the time (L3-08).
    env = ap.add_mutually_exclusive_group(required=True)
    env.add_argument("--demo", action="store_true")
    env.add_argument("--live", action="store_true")
    args = ap.parse_args(argv)

    egress = check_egress()
    if not egress.allowed:
        print(f"REFUSING: {egress.reason}", file=sys.stderr)
        return 2
    settings = load()
    demo = args.demo
    settings = settings.model_copy(update={"venue": "okx_demo" if demo else "okx_live"})
    creds = settings.creds()

    try:
        pos_body = _signed_get(OKX.auth_base_url, POSITIONS, creds, demo)
        algo_body = _signed_get(OKX.auth_base_url, ALGOS, creds, demo)
    except Exception as e:
        print(f"could not read the account: {e}", file=sys.stderr)
        return 2
    for body in (pos_body, algo_body):
        if str(body.get("code")) != "0":
            print(f"account read failed: {body.get('code')} {body.get('msg')}", file=sys.stderr)
            return 2

    positions = [p for p in pos_body.get("data", []) if float(p.get("pos") or 0)]
    stops = {
        str(o.get("instId")): str(o.get("slTriggerPx"))
        for o in algo_body.get("data", [])
        if o.get("slTriggerPx")
    }
    name = "journal.db" if not demo else "journal-S003-demo.db"
    print(report(positions, stops, protect_gaps(settings.data_dir / name)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
