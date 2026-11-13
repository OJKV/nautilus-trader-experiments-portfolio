"""Read-only venue snapshot: open positions, open orders, open algo (stop) orders.

Used after a rehearsal or drill to prove the account was left flat, and by
`deploy/update.sh` to decide whether it may restart the node. GET only; never
places, amends or cancels anything. Usage:

    python -m ntx.ops.venue_state --demo

Exit codes (a deploy script reads these): **0** flat · **1** something is open ·
**2** the venue could not be read, or egress is refused.

Two defects this file had until 2026-09-19 (review L-12), both of which made it
claim FLAT when it knew nothing:

* an error body has no `data`, which produced zero rows, printed FLAT and exited
  0 (R18, R31) -- the tool the runbooks use to confirm state was the tool most
  likely to lie during an incident;
* only `ordType=conditional` was queried, so an OCO child (the attached-stop
  mode) or a plain trigger order was invisible.
"""

from __future__ import annotations

import argparse
import sys

from ntx.config.geo import check_egress
from ntx.config.settings import load
from ntx.config.venues import OKX
from ntx.doctor import _signed_get

# OKX returns algo orders per ordType; one call per family, or they stay hidden.
ALGO_TYPES = ("conditional", "oco", "trigger", "move_order_stop")

PATHS = {
    "positions": "/api/v5/account/positions?instType=SWAP",
    "open_orders": "/api/v5/trade/orders-pending?instType=SWAP",
    **{
        f"open_algo_{t}": f"/api/v5/trade/orders-algo-pending?instType=SWAP&ordType={t}"
        for t in ALGO_TYPES
    },
}


def _open_rows(label: str, body: dict) -> list[dict]:
    rows = body.get("data") or []
    if label != "positions":
        return list(rows)
    out = []
    for r in rows:
        try:
            if float(r.get("pos") or 0) != 0:
                out.append(r)
        except (TypeError, ValueError):
            out.append(r)  # unparseable size: treat as open, never as flat
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    # Required: a flagless run used to follow .env, which on the box names the
    # OTHER account half the time (L3-08).
    env = ap.add_mutually_exclusive_group(required=True)
    env.add_argument("--demo", action="store_true")
    # Explicit --live: `.env` points at the demo account during the demo phase,
    # and "prove the LIVE account is flat" is the first step of a live deploy.
    env.add_argument("--live", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    args = ap.parse_args(argv)
    egress = check_egress()
    if not egress.allowed:
        print(f"REFUSING: {egress.reason}", file=sys.stderr)
        return 2
    settings = load()
    demo = args.demo
    settings = settings.model_copy(update={"venue": "okx_demo" if demo else "okx_live"})
    creds = settings.creds()
    dirty = 0
    for label, path in PATHS.items():
        try:
            body = _signed_get(OKX.auth_base_url, path, creds, demo)
        except Exception as e:
            print(f"{label:<18} ERROR {e}", file=sys.stderr)
            return 2
        if str(body.get("code")) != "0":
            # Never print FLAT over an error: this is what the runbooks trust.
            print(
                f"{label:<18} FAILED code={body.get('code')} msg={body.get('msg')}",
                file=sys.stderr,
            )
            return 2
        rows = _open_rows(label, body)
        dirty += len(rows)
        if not args.quiet:
            print(f"{label:<18} {len(rows)}  (code={body.get('code')})")
            for r in rows:
                print(
                    f"    {r.get('instId')} pos={r.get('pos', '')} sz={r.get('sz', '')} "
                    f"side={r.get('side', r.get('posSide', ''))} "
                    f"ordType={r.get('ordType', '')} "
                    f"trigger={r.get('slTriggerPx') or r.get('triggerPx', '')}"
                )
    if not args.quiet:
        print("FLAT" if dirty == 0 else f"NOT FLAT: {dirty} open item(s)")
    return 0 if dirty == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
