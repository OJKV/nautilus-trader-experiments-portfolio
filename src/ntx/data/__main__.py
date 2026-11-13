"""`make data` -- build the backtest catalog.

Usage:
    python -m ntx.data                       # full universe, 2020-01 -> last month
    python -m ntx.data --assets BTC SOL      # subset
    python -m ntx.data --start 2024-01       # shorter window
    python -m ntx.data --refresh-specs       # re-fetch OKX instrument specs
"""

from __future__ import annotations

import argparse
import logging
import sys

from ntx.config.settings import load
from ntx.config.universe import BACKTEST_START, BY_BASE, TIMEFRAMES, UNIVERSE
from ntx.data import binance_dumps as dumps
from ntx.data import catalog as cat
from ntx.data.instruments import build_all


def main() -> int:
    ap = argparse.ArgumentParser(description="build the ntx backtest catalog")
    ap.add_argument("--start", default=BACKTEST_START, help="first month, YYYY-MM")
    ap.add_argument("--end", default=None, help="last month, YYYY-MM (default: last complete)")
    ap.add_argument("--assets", nargs="*", default=None, help="base symbols, e.g. BTC SOL")
    ap.add_argument("--timeframes", nargs="*", default=list(TIMEFRAMES))
    ap.add_argument("--refresh-specs", action="store_true")
    ap.add_argument("--no-funding", action="store_true")
    ap.add_argument(
        "--rebuild", action="store_true", help="delete existing bar data for these assets first"
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    s = load()

    assets = UNIVERSE
    if args.assets:
        unknown = [a for a in args.assets if a.upper() not in BY_BASE]
        if unknown:
            print(f"unknown assets: {unknown}; known: {sorted(BY_BASE)}", file=sys.stderr)
            return 2
        assets = tuple(BY_BASE[a.upper()] for a in args.assets)

    months = dumps.months(args.start, args.end)
    print(
        f"catalog: {len(assets)} assets x {len(months)} months "
        f"({months[0]} -> {months[-1]}), timeframes {args.timeframes}"
    )

    instruments = build_all(
        s.data_dir / "okx_specs.json", s.okx_public_base_url, refresh=args.refresh_specs
    )
    stats = cat.build(
        data_dir=s.data_dir,
        instruments=instruments,
        months=months,
        timeframes=tuple(args.timeframes),
        assets=assets,
        with_funding=not args.no_funding,
        rebuild=args.rebuild,
    )

    print("\n" + "=" * 118)
    for st in stats:
        print(st.summary())
    print("=" * 118)
    rejected = sum(st.months_rejected for st in stats)
    gaps = sum(st.total_gaps for st in stats)
    print(
        f"1m bars downloaded this run: {sum(st.minute_bars for st in stats):,} | "
        f"rejected months: {rejected} | sub-hour gaps tolerated: {gaps}"
    )
    if rejected:
        print("FAILED: some symbol-months failed integrity and were excluded")
    return 1 if rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
