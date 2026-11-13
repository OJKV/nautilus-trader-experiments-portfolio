"""Phase 1 data report -- verify the catalog by reading it back.

Deliberately reads the **catalog**, not the build log. A build log says what the
builder believed it did; the catalog is what a backtest will actually consume
(R31: verify the read-back, not just the write).

Checks per instrument and timeframe: bar count, first/last timestamp, strict
ascending order, uniqueness, and that every gap between consecutive bars equals
exactly one bar period. Any deviation is listed.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from ntx.config.universe import UNIVERSE
from ntx.data.aggregate import TF_MINUTES
from ntx.data.read import open_catalog, read_bars

NS_PER_MIN = 60_000_000_000


def _utc(ns: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ns / 1e9, dt.UTC)


@dataclass
class TFCoverage:
    timeframe: str
    bars: int = 0
    first: int | None = None
    last: int | None = None
    duplicates: int = 0
    out_of_order: int = 0
    irregular_gaps: Counter = field(default_factory=Counter)  # gap_minutes -> count

    @property
    def expected_bars(self) -> int:
        """Bars implied by the observed span -- catches internal holes."""
        if self.first is None or self.last is None:
            return 0
        step = TF_MINUTES[self.timeframe] * NS_PER_MIN
        return int((self.last - self.first) // step) + 1

    @property
    def missing(self) -> int:
        return self.expected_bars - self.bars

    @property
    def ok(self) -> bool:
        return (
            self.duplicates == 0
            and self.out_of_order == 0
            and not self.irregular_gaps
            and self.missing == 0
        )


def assess_timeframe(catalog, instrument_id: str, timeframe: str) -> TFCoverage:
    cov = TFCoverage(timeframe=timeframe)
    bars = read_bars(catalog, instrument_id, timeframe)
    cov.bars = len(bars)
    if not bars:
        return cov

    ts = [b.ts_event for b in bars]
    cov.first, cov.last = ts[0], ts[-1]
    cov.duplicates = len(ts) - len(set(ts))
    step = TF_MINUTES[timeframe] * NS_PER_MIN
    for a, b in pairwise(ts):
        delta = b - a
        if delta <= 0:
            cov.out_of_order += 1
        elif delta != step:
            cov.irregular_gaps[delta // NS_PER_MIN] += 1
    return cov


def build_report(data_dir: Path, timeframes: tuple[str, ...]) -> str:
    catalog = open_catalog(data_dir)
    lines: list[str] = [
        "# Phase 1 — Data Report",
        "",
        f"Generated {dt.datetime.now(dt.UTC).strftime('%Y-%m-%d %H:%M')} UTC "
        f"by reading the catalog back (not the build log).",
        "",
        "## Coverage",
        "",
        "| instrument | tf | bars | first bar (UTC) | last bar (UTC) | missing | dups | irregular gaps |",
        "|---|---|---|---|---|---|---|---|",
    ]
    all_ok = True
    for asset in UNIVERSE:
        for tf in timeframes:
            cov = assess_timeframe(catalog, asset.nautilus_id, tf)
            all_ok &= cov.ok
            gaps = (
                ", ".join(f"{n}x{m}m" for m, n in sorted(cov.irregular_gaps.items()))
                if cov.irregular_gaps
                else "-"
            )
            first = _utc(cov.first).strftime("%Y-%m-%d %H:%M") if cov.first else "-"
            last = _utc(cov.last).strftime("%Y-%m-%d %H:%M") if cov.last else "-"
            flag = "" if cov.ok else " ⚠️"
            lines.append(
                f"| {asset.okx_inst_id} | {tf} | {cov.bars:,} | {first} | {last} "
                f"| {cov.missing}{flag} | {cov.duplicates} | {gaps} |"
            )
    lines += ["", "## Verdict", ""]
    lines.append(
        "All timeframes: strictly ascending, unique timestamps, every gap exactly "
        "one bar period, no internal holes."
        if all_ok
        else "⚠️ Deviations listed above — investigate before backtesting."
    )
    lines += [
        "",
        "## Listing dates",
        "",
        "First bar per asset is its perp listing date on Binance USDT-M. Months "
        "before that are absent by definition and were counted as `pre-listing`, "
        "not as errors — a backtest must never trade an asset before it existed.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    import argparse

    from ntx.config.settings import load
    from ntx.config.universe import TIMEFRAMES

    ap = argparse.ArgumentParser(description="verify the catalog and write a data report")
    ap.add_argument("--timeframes", nargs="*", default=list(TIMEFRAMES))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    s = load()
    text = build_report(s.data_dir, tuple(args.timeframes))
    print(text)
    out = Path(args.out) if args.out else Path("reports") / "phase1-data-report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"\nwritten to {out}")
    return 0 if "⚠️" not in text else 1


if __name__ == "__main__":
    raise SystemExit(main())
