"""One recorded cross-venue snapshot for docs/strategies/research/arbitrage.md §2.3 (R42).

    python -m ntx.experiments.arb_snapshot

Funding (annualised) on Binance/OKX/Bybit, spot mid gaps across venues and dated-futures
basis for our 10 coins, from PUBLIC endpoints only (no keys). A snapshot is one moment:
it shows structure, never an edge -- the arbitrage P0s in §6 are what test edges.
"""

from __future__ import annotations

import contextlib
import io
import runpy
import sys

from ntx.backtest.manifest import record


def main() -> int:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        runpy.run_module("ntx.experiments.arb_snapshot_raw", run_name="__main__")
    out = buf.getvalue()
    print(out)
    path = record(
        label="arb-snapshot",
        spec={"source": "public REST: Binance fapi, OKX v5 public, Bybit v5 market"},
        extra={"script": "ntx.experiments.arb_snapshot", "kind": "snapshot"},
        stdout=out,
    )
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
