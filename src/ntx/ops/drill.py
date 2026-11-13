"""Failure drills on the DEMO account (gap G2). Refuses to touch live.

python -m ntx.ops.drill cancel-stop --inst SOL   # simulate a stop cancelled by hand
"""

from __future__ import annotations

import argparse
import sys

from ntx.config.geo import check_egress
from ntx.config.venues import OKX
from ntx.doctor import _signed_get
from ntx.ops.flatten import _settings, _signed_post


def open_stops(inst_id: str) -> list[dict]:
    r = _signed_get(
        OKX.auth_base_url,
        f"/api/v5/trade/orders-algo-pending?ordType=conditional&instId={inst_id}",
        _settings(True).creds(),
        True,
    )
    return r.get("data", [])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cancel-stop")
    c.add_argument("--inst", required=True, help="base asset, e.g. SOL")
    args = ap.parse_args(argv)
    if not check_egress().allowed:
        print("REFUSING: egress not allowed", file=sys.stderr)
        return 2
    inst_id = f"{args.inst.upper()}-USDT-SWAP"
    stops = open_stops(inst_id)
    if not stops:
        print(f"no open stop on {inst_id}")
        return 1
    body = [{"algoId": s["algoId"], "instId": inst_id} for s in stops]
    r = _signed_post(
        OKX.auth_base_url, "/api/v5/trade/cancel-algos", body, _settings(True).creds(), True
    )
    print(
        f"cancelled {len(body)} stop(s) on {inst_id} (demo): code={r.get('code')} {r.get('msg', '')}"
    )
    return 0 if r.get("code") == "0" else 1


if __name__ == "__main__":
    raise SystemExit(main())
