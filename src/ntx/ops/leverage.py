"""Per-instrument leverage at OKX: read it, pin it, and refuse to trade if it drifted.

Why this exists (measured, 2026-09-19): `NTX_MAX_LEVERAGE=3.0` is a **local
sizing ceiling** only -- nothing in this repo, and nothing in nautilus's OKX
adapter, ever told OKX what leverage to use. The venue keeps its own number
**per instrument**, and in isolated margin that number alone sets the
liquidation price:

| leverage | liquidation is this far from entry |
|---|---|
| 1x | ~99% |
| 2x | ~49% |
| **3x** | **~33%** |
| 10x | ~9.6% |
| 20x | ~4.6% |
| 100x | ~0.55% |

Our stop is 2xATR on 4h bars, which for these alts is 4-8% of price. At 3x the
stop is hit 4-8x before liquidation. At OKX's higher defaults, liquidation moves
*inside* the tail of our own stop distance -- the exchange would close the trade
first, at a worse price, and our protective stop would never fire.

    python -m ntx.ops.leverage --check          # read, compare, exit 1 on drift
    python -m ntx.ops.leverage --set --demo     # pin every traded instrument
    python -m ntx.ops.leverage --set --live     # same, typed confirmation

Setting leverage moves no money and is reversible, but OKX refuses it while
orders or positions exist on that instrument (59101/59112), and lowering it on
an open position can fail for margin (59109). So this tool refuses to run unless
the account is flat.
"""

from __future__ import annotations

import argparse
import sys

from ntx.config.settings import Settings, load
from ntx.config.universe import UNIVERSE
from ntx.config.venues import OKX

CONFIRM = "SET LEVERAGE"
MGN_MODE = "isolated"  # every order carries tdMode=isolated (node.py margin_mode)


def _for(settings: Settings, demo: bool) -> Settings:
    """Settings whose `creds()` match the environment we are about to call.

    `creds()` picks by `NTX_VENUE`, while `demo` sets the `x-simulated-trading`
    header. Letting those two disagree is OKX error 50101 "APIKey does not match
    current environment" -- which is exactly what `--check --demo` did on a box
    whose .env says okx_live (F050).
    """
    return settings.model_copy(update={"venue": "okx_demo" if demo else "okx_live"})


def read_leverage(
    instrument_ids: list[str], settings: Settings, demo: bool, base: str | None = None
) -> dict[str, float]:
    """instId -> configured leverage. OKX takes up to 20 ids per call."""
    from ntx.doctor import _signed_get

    settings = _for(settings, demo)
    base = base or OKX.auth_base_url
    out: dict[str, float] = {}
    ids = [i.split(".")[0] for i in instrument_ids]  # SOL-USDT-SWAP.OKX -> SOL-USDT-SWAP
    for chunk in (ids[i : i + 20] for i in range(0, len(ids), 20)):
        path = f"/api/v5/account/leverage-info?instId={','.join(chunk)}&mgnMode={MGN_MODE}"
        body = _signed_get(base, path, settings.creds(), demo)
        if str(body.get("code")) != "0":
            raise RuntimeError(f"cannot read leverage: {body.get('code')} {body.get('msg')}")
        for row in body.get("data") or []:
            try:
                out[row["instId"]] = float(row["lever"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def set_leverage(
    instrument_id: str, lever: float, settings: Settings, demo: bool, base: str | None = None
) -> dict:
    """Pin one instrument. `posSide` is omitted deliberately: it is only valid in
    long/short position mode, and this account is `net_mode`."""
    from ntx.ops.flatten import _signed_post

    settings = _for(settings, demo)
    inst = instrument_id.split(".")[0]
    body = {"instId": inst, "lever": str(int(lever)), "mgnMode": MGN_MODE}
    return _signed_post(
        base or OKX.auth_base_url, "/api/v5/account/set-leverage", body, settings.creds(), demo
    )


def drift(
    configured: dict[str, float], target: float, instrument_ids: list[str]
) -> dict[str, float | None]:
    """Instruments whose venue leverage is not the target. None = unknown."""
    out: dict[str, float | None] = {}
    for full in instrument_ids:
        inst = full.split(".")[0]
        have = configured.get(inst)
        if have is None or abs(have - target) > 1e-9:
            out[inst] = have
    return out


def _universe_ids(only: str | None) -> list[str]:
    if only:
        from ntx.live.node import resolve_instruments

        return list(resolve_instruments(only))
    return [a.nautilus_id for a in UNIVERSE]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="read or pin per-instrument OKX leverage")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="read and compare, change nothing")
    mode.add_argument("--set", action="store_true", help="pin every instrument to the target")
    # Required: a flagless run used to follow .env, which on the box names the
    # OTHER account half the time (L3-08).
    env = ap.add_mutually_exclusive_group(required=True)
    env.add_argument("--demo", action="store_true")
    env.add_argument("--live", action="store_true")
    ap.add_argument("--lever", type=float, default=None, help="default: NTX_MAX_LEVERAGE")
    ap.add_argument("--only", default=None, help="comma bases, e.g. SOL,DOGE")
    args = ap.parse_args(argv)

    settings = load()
    demo = args.demo
    target = args.lever if args.lever is not None else settings.max_leverage
    ids = _universe_ids(args.only)
    where = "DEMO" if demo else "LIVE"

    try:
        configured = read_leverage(ids, settings, demo)
    except Exception as e:
        print(f"cannot read leverage from {where}: {e}", file=sys.stderr)
        return 2

    print(f"{where} leverage, target {target:g}x {MGN_MODE}:")
    for full in ids:
        inst = full.split(".")[0]
        have = configured.get(inst)
        mark = "ok " if have is not None and abs(have - target) < 1e-9 else "DRIFT"
        print(f"  [{mark}] {inst:<22} {have if have is not None else '?'}")
    bad = drift(configured, target, ids)

    if args.check:
        if bad:
            print(f"\n{len(bad)} instrument(s) not at {target:g}x: {', '.join(sorted(bad))}")
            print(f"fix with: python -m ntx.ops.leverage --set --{'demo' if demo else 'live'}")
            return 1
        print("\nall instruments are at the target")
        return 0

    if not bad:
        print("\nnothing to do")
        return 0

    # OKX refuses a change while orders or positions exist (59101/59112), and a
    # lower leverage on an open position can fail for margin (59109).
    from ntx.ops.venue_state import main as venue_state

    state = venue_state(["--demo"] if demo else ["--live"])  # never .env's account (L2-07)
    if state != 0:
        print("account is not flat (or unreadable) -- refusing to change leverage", file=sys.stderr)
        return 2

    if not demo:
        typed = input(
            f"LIVE ACCOUNT. Type {CONFIRM} to pin {len(bad)} instrument(s) to {target:g}x: "
        )
        if typed.strip() != CONFIRM:
            print("not confirmed -- nothing changed")
            return 2

    failed = 0
    for inst in sorted(bad):
        r = set_leverage(inst, target, settings, demo)
        ok = str(r.get("code")) == "0"
        failed += not ok
        print(f"set {inst} -> {target:g}x: {'OK' if ok else r}")
    if failed:
        return 1
    after = read_leverage(ids, settings, demo)
    still = drift(after, target, ids)
    if still:  # R31: verify the read-back
        print(f"STILL NOT AT TARGET after setting: {', '.join(sorted(still))}", file=sys.stderr)
        return 1
    print("verified: every instrument reads back at the target")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
