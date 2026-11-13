"""Build the backtest ParquetDataCatalog.

Pipeline:

    data.binance.vision 1m zips        (price series, free, no tunnel)
              |  checksum-verified, header-sniffed
              v
    integrity.check()                  (duplicates, gaps, OHLC sanity)
              |  fatal problems abort that symbol-month
              v
    aggregate() -> 15m / 1h / 4h       (exact; no interpolation)
              |
              v
    nautilus Bar objects keyed to OKX instrument ids
              |                        (specs from OKX -- ADR-0005)
              v
    ParquetDataCatalog  +  FundingRateUpdate series

Bars carry OKX instrument ids because that is where orders go; only the price
series is Binance's. Cross-venue basis on these majors measured under 1bp.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from ntx.config.universe import UNIVERSE, Asset
from ntx.data import binance_dumps as dumps
from ntx.data import integrity
from ntx.data.aggregate import TF_MINUTES, aggregate
from ntx.data.binance_dumps import Kline

log = logging.getLogger(__name__)

MS_TO_NS = 1_000_000

_TF_SPEC = {
    "1m": "1-MINUTE",
    "5m": "5-MINUTE",
    "15m": "15-MINUTE",
    "30m": "30-MINUTE",
    "1h": "1-HOUR",
    "4h": "4-HOUR",
    "1d": "1-DAY",
}
_SPEC_TO_TF = {v: k for k, v in _TF_SPEC.items()}


def _bar_period_ns(bar_type: BarType) -> int:
    """Bar period in nanoseconds, derived from the BarType itself.

    Taken from the bar type rather than passed in, so a bar can never be
    stamped with the wrong period's close time.
    """
    spec = str(bar_type).split(".")[-1]
    for name, tf in _SPEC_TO_TF.items():
        if f"-{name}-" in f"-{spec}-":
            return TF_MINUTES[tf] * 60 * 1_000_000_000
    raise ValueError(f"cannot derive a bar period from {bar_type}")


def bar_type_for(inst: CryptoPerpetual, timeframe: str) -> BarType:
    """BarType string, e.g. 'SOL-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL'.

    EXTERNAL because bars arrive pre-aggregated rather than being built by the
    engine from ticks.
    """
    return BarType.from_str(f"{inst.id}-{_TF_SPEC[timeframe]}-LAST-EXTERNAL")


def to_bars(klines: list[Kline], inst: CryptoPerpetual, bar_type: BarType) -> list[Bar]:
    """Convert klines to nautilus Bars using the instrument's own precision.

    `make_price` / `make_qty` are used rather than `round()` -- R17. Binance
    prices can carry more precision than OKX's tick size, and rounding by hand
    is how one production bot sent an order for zero ETH 68 times in a row.

    **`ts_event` is the bar's CLOSE time, not its open** (F014). The engine
    delivers a bar at its `ts_event` and lets the strategy act on it there. With
    the open time, the strategy received the 02:00-03:00 bar at 02:00 and could
    read its close before it had happened -- direct lookahead bias. Verified by
    probe and asserted by `test_bar_ts_event_is_close_time`.
    """
    period_ns = _bar_period_ns(bar_type)
    out: list[Bar] = []
    for k in klines:
        close_ns = k.open_time * MS_TO_NS + period_ns
        out.append(
            Bar(
                bar_type=bar_type,
                open=inst.make_price(k.open),
                high=inst.make_price(k.high),
                low=inst.make_price(k.low),
                close=inst.make_price(k.close),
                volume=inst.make_qty(k.volume),
                ts_event=close_ns,
                ts_init=close_ns,
            )
        )
    return out


@dataclass
class BuildStats:
    symbol: str
    months_found: int = 0
    months_missing: int = 0  # before listing -- expected, not an error
    months_rejected: int = 0  # failed integrity
    months_skipped: int = 0  # already in the catalog
    days_filled: int = 0  # repaired from daily archives
    minute_bars: int = 0
    bars_written: dict[str, int] = field(default_factory=dict)
    funding_points: int = 0
    first_month: str | None = None
    last_month: str | None = None
    reports: list[integrity.Report] = field(default_factory=list)

    @property
    def total_gaps(self) -> int:
        return sum(len(r.gaps) for r in self.reports)

    @property
    def worst_gap_minutes(self) -> int:
        return max((r.largest_gap_minutes for r in self.reports), default=0)

    def summary(self) -> str:
        wrote = ", ".join(f"{tf}={n}" for tf, n in sorted(self.bars_written.items()))
        span = f"{self.first_month}..{self.last_month}" if self.first_month else "-"
        return (
            f"{self.symbol:<9} {span:<18} months: {self.months_found} built, "
            f"{self.months_missing} pre-listing, {self.months_skipped} cached, "
            f"{self.months_rejected} rejected"
            + (f", {self.days_filled}d filled" if self.days_filled else "")
            + f" | 1m={self.minute_bars:>7} | {wrote} "
            f"| funding={self.funding_points} | gaps={self.total_gaps}"
            + (f" (worst {self.worst_gap_minutes}m)" if self.total_gaps else "")
        )


def _month_span_ns(month: str, timeframe: str) -> tuple[int, int]:
    """(first bar ts, last bar ts) for a complete month at `timeframe`, in ns.

    The catalog records an interval by its first and **last bar timestamp**, not
    by month boundaries -- a full March of 15m bars ends at 23:45 on the 31st,
    1h at 23:00, 4h at 20:00. Comparing against month-end instead of the last
    bucket start is what made the first coverage check always fail (F011).
    """
    import calendar
    from datetime import UTC, datetime

    y, m = (int(x) for x in month.split("-"))
    start = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1e9)
    days = calendar.monthrange(y, m)[1]
    bucket_ns = TF_MINUTES[timeframe] * 60 * 1_000_000_000
    month_end_exclusive = start + days * 86_400 * 1_000_000_000
    return start, month_end_exclusive - bucket_ns


def _covered(intervals: list[tuple[int, int]], month: str, timeframe: str) -> bool:
    """True if an existing catalog interval already spans this month-timeframe."""
    start_ns, last_ns = _month_span_ns(month, timeframe)
    return any(lo <= start_ns and hi >= last_ns for lo, hi in intervals)


def build_symbol(
    asset: Asset,
    inst: CryptoPerpetual,
    *,
    raw_dir: Path,
    catalog: ParquetDataCatalog,
    months: list[str],
    timeframes: tuple[str, ...],
    with_funding: bool = True,
) -> BuildStats:
    """Download, verify, aggregate and write one symbol, **one month at a time**.

    Per-month writes rather than one giant write, because:

    1. `ParquetDataCatalog` requires **disjoint** time intervals per bar type and
       raises `ValueError: would create non-disjoint intervals` otherwise. A
       single 2020-2026 write collides with anything already present (F011).
    2. It makes the build **resumable and idempotent** -- an interrupted download
       resumes instead of restarting, and re-running skips cached months.

    Per-month aggregation is exact: 15m, 1h and 4h all divide a day evenly and
    months begin at 00:00 UTC, so no bucket ever spans a month boundary.
    """
    st = BuildStats(symbol=asset.binance_symbol)
    existing = {tf: catalog.get_intervals(Bar, str(bar_type_for(inst, tf))) for tf in timeframes}

    for month in months:
        if all(_covered(existing[tf], month, tf) for tf in timeframes):
            st.months_skipped += 1
            if st.first_month is None:
                st.first_month = month
            st.last_month = month
            continue

        # Repair truncated monthly archives from the daily ones. Binance's
        # monthly dumps are sometimes short while the dailies are complete --
        # SOL/XRP/TRX each lost 3 days of 2022-02 and 2 days of 2022-04 that
        # way, and gap detection alone could not see it (F013).
        kl, filled = dumps.fetch_month_complete(
            asset.binance_symbol,
            month,
            raw_dir,
            # Before the asset's first data, an absent month is the listing gap,
            # not a hole worth probing 60 daily archives for.
            probe_dailies_when_month_absent=st.first_month is not None,
        )
        if not kl:
            st.months_missing += 1  # not listed yet -- legitimate
            continue
        if filled:
            st.days_filled += len(filled)
            log.info(
                "%s %s: filled %d day(s) from daily archives: %s",
                asset.binance_symbol,
                month,
                len(filled),
                ", ".join(filled),
            )

        # A late start in the asset's FIRST month is the listing date, not a
        # truncation, so it must not be treated as a fatal defect.
        rep = integrity.check(
            kl, asset.binance_symbol, month, is_first_month=st.first_month is None
        )
        st.reports.append(rep)
        if not rep.ok:
            st.months_rejected += 1
            log.warning("rejected %s", rep.summary())
            continue

        kl.sort(key=lambda k: k.open_time)
        st.months_found += 1
        st.minute_bars += len(kl)
        if st.first_month is None:
            st.first_month = month
        st.last_month = month

        for tf in timeframes:
            if _covered(existing[tf], month, tf):
                continue
            res = aggregate(kl, tf)
            bars = to_bars(res.bars, inst, bar_type_for(inst, tf))
            if bars:
                catalog.write_data(bars)
                st.bars_written[tf] = st.bars_written.get(tf, 0) + len(bars)
            if res.incomplete_buckets:
                log.info(
                    "%s %s %s: %d incomplete buckets (%d minutes missing)",
                    asset.binance_symbol,
                    month,
                    tf,
                    res.incomplete_buckets,
                    res.minutes_missing,
                )

        if with_funding:
            fr = dumps.fetch_funding(asset.binance_symbol, month, raw_dir)
            if fr:
                st.funding_points += len(fr)
    return st


def build(
    *,
    data_dir: Path,
    instruments: dict[str, CryptoPerpetual],
    months: list[str],
    timeframes: tuple[str, ...],
    assets: tuple[Asset, ...] = UNIVERSE,
    with_funding: bool = True,
    rebuild: bool = False,
) -> list[BuildStats]:
    """Build the whole catalog. Idempotent: cached archives and months are reused."""
    raw_dir = data_dir / "raw"
    catalog_dir = data_dir / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(catalog_dir))

    out: list[BuildStats] = []
    for asset in assets:
        inst = instruments.get(asset.okx_inst_id)
        if inst is None:
            log.warning("no OKX instrument for %s, skipping", asset.okx_inst_id)
            continue
        if rebuild:
            for tf in timeframes:
                bt = str(bar_type_for(inst, tf))
                target = catalog_dir / "data" / "bar" / bt
                if target.exists():
                    shutil.rmtree(target)
                    log.info("rebuild: cleared %s", bt)
        catalog.write_data([inst])
        st = build_symbol(
            asset,
            inst,
            raw_dir=raw_dir,
            catalog=catalog,
            months=months,
            timeframes=timeframes,
            with_funding=with_funding,
        )
        log.info("%s", st.summary())
        out.append(st)
    return out
