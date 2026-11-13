"""Flatten OKX positions: close each SWAP position and cancel its orders.

Uses `POST /api/v5/trade/close-position` with `autoCxl=true`, which closes the
whole position and cancels its pending orders (stops included) in one call.

    python -m ntx.ops.flatten --demo [--only SOL,DOGE]
    python -m ntx.ops.flatten --live [--only SOL]     # kill switch (G13)

`--live` lists what it will close and requires typing FLATTEN LIVE. Stop the
bot first (`systemctl stop ntx-s003`), or it may re-enter on its next signal.
"""

from __future__ import annotations

import argparse
import sys

from ntx.config.geo import check_egress
from ntx.config.settings import load
from ntx.config.venues import OKX
from ntx.doctor import _signed_get


def _signed_post(base: str, path: str, body: dict, creds: tuple[str, str, str], demo: bool) -> dict:
    """Authenticated POST. Credentials travel on curl's stdin, never in the argv
    (L-14) -- see `ntx.ops.okx_rest`."""
    from ntx.ops.okx_rest import signed

    return signed("POST", base, path, creds, demo, body=body)


CONFIRM = "FLATTEN LIVE"


def live_confirmed(typed: str | None) -> bool:
    return typed == CONFIRM


def select(rows: list[dict], only: set[str] | None) -> list[dict]:
    """Non-zero positions, optionally limited to base assets (`SOL` -> `SOL-USDT-SWAP`)."""
    out = []
    for r in rows:
        if not float(r.get("pos") or 0):
            continue
        if only is not None and r["instId"].split("-")[0] not in only:
            continue
        out.append(r)
    return out


def _settings(demo: bool):
    s = load()
    return s.model_copy(update={"venue": "okx_demo" if demo else "okx_live"})


def _positions(demo: bool) -> list[dict]:
    r = _signed_get(
        OKX.auth_base_url, "/api/v5/account/positions?instType=SWAP", _settings(demo).creds(), demo
    )
    if r.get("code") != "0":
        raise RuntimeError(f"cannot read positions: {r}")
    return r["data"]


def _egress_ok() -> bool:
    return check_egress().allowed


def egress_gate(*, skip: bool, check=_egress_ok) -> bool:
    """May we send orders from this box?

    The check is fail-closed on purpose (a free VPN can surface in an
    OKX-restricted country), but it depends on a third-party lookup -- so during
    an incident, exactly when networks misbehave, the kill switch refused to run
    (L-06). `--skip-egress-check` is the documented override; the typed
    `FLATTEN LIVE` confirmation still applies.
    """
    if skip:
        print("egress check skipped (--skip-egress-check)", file=sys.stderr)
        return True
    return bool(check())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    env = ap.add_mutually_exclusive_group(required=True)
    env.add_argument("--demo", action="store_true")
    env.add_argument("--live", action="store_true")
    ap.add_argument("--only", default=None, help="comma base assets, e.g. SOL,DOGE")
    ap.add_argument(
        "--skip-egress-check",
        action="store_true",
        help="close positions even if the country lookup fails (incident use)",
    )
    args = ap.parse_args(argv)
    demo = args.demo
    if not egress_gate(skip=args.skip_egress_check):
        print(
            "REFUSING: egress not allowed (re-run with --skip-egress-check if this "
            "is an incident and you know where the box is)",
            file=sys.stderr,
        )
        return 2
    only = {x.strip().upper() for x in args.only.split(",")} if args.only else None
    targets = select(_positions(demo), only)
    if not targets:
        print("nothing to close")
        return 0
    for p in targets:
        print(f"  {p['instId']} pos={p['pos']} mgnMode={p['mgnMode']}")
    if not demo:
        typed = input(f"LIVE ACCOUNT. Type {CONFIRM} to close the positions above: ")
        if not live_confirmed(typed):
            print("not confirmed -- nothing sent")
            return 2
    creds = _settings(demo).creds()
    failed = 0
    for p in targets:
        body = {"instId": p["instId"], "mgnMode": p["mgnMode"], "autoCxl": True}
        if p.get("posSide") and p["posSide"] != "net":
            body["posSide"] = p["posSide"]
        r = _signed_post(OKX.auth_base_url, "/api/v5/trade/close-position", body, creds, demo)
        ok = r.get("code") == "0"
        failed += not ok
        print(f"close {p['instId']} pos={p['pos']}: {'OK' if ok else r}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
