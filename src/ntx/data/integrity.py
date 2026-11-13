"""Data integrity checks. Everything downstream inherits this data's flaws.

The failure being prevented (docs/13 §1, pattern 6): duplicate rows silently
accumulated in one production system until 47-51% of its candles were duplicates
-- so every indicator, every signal and every trade decision for months was
computed on corrupt data, with no error raised anywhere.

Checks are pure functions over a list of Klines so they need no network, no
database and no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

from ntx.data.binance_dumps import Kline

MINUTE_MS = 60_000


@dataclass
class Report:
    symbol: str
    month: str
    bars: int = 0
    expected_bars: int | None = None
    duplicates: int = 0
    gaps: list[tuple[int, int]] = field(default_factory=list)  # (after_ts, missing_bars)
    ohlc_violations: list[int] = field(default_factory=list)  # open_time of bad bars
    nonpositive_volume: int = 0
    out_of_order: int = 0
    nonfinite: int = 0

    @property
    def largest_gap_minutes(self) -> int:
        return max((n for _, n in self.gaps), default=0)

    # Days present in the archive. A month that simply STOPS early has no
    # internal gap, so gap detection alone cannot see it -- that is how three
    # assets kept silently-truncated months through the first build (F013).
    days_present: int = 0
    days_expected: int = 0
    is_first_month: bool = False

    @property
    def missing_days(self) -> int:
        return max(0, self.days_expected - self.days_present)

    @property
    def ok(self) -> bool:
        """Fatal conditions only.

        A gap of a few minutes is a real Binance outage and is tolerated; an hour
        or more is not, because a 1h bar built across it would be wrong rather
        than merely missing.

        A truncated month is fatal too -- unless this is the asset's first month,
        where a late start is simply the listing date.
        """
        truncated = self.missing_days > 0 and not self.is_first_month
        return (
            self.duplicates == 0
            and not self.ohlc_violations
            and self.out_of_order == 0
            and self.nonfinite == 0
            and self.largest_gap_minutes < 60
            and not truncated
        )

    def summary(self) -> str:
        flag = "ok  " if self.ok else "FAIL"
        return (
            f"[{flag}] {self.symbol} {self.month}: {self.bars} bars"
            + (f"/{self.expected_bars}" if self.expected_bars else "")
            + f", dups={self.duplicates}, gaps={len(self.gaps)}"
            + (f" (worst {self.largest_gap_minutes}m)" if self.gaps else "")
            + f", ohlc_bad={len(self.ohlc_violations)}"
            + (f", MISSING {self.missing_days}d" if self.missing_days else "")
            + (f", nonfinite={self.nonfinite}" if self.nonfinite else "")
        )


def _expected_bars(month: str) -> int:
    """Minutes in the month -- the bar count a complete archive should have."""
    import calendar

    y, m = (int(x) for x in month.split("-"))
    return calendar.monthrange(y, m)[1] * 1440


def check(klines: list[Kline], symbol: str, month: str, *, is_first_month: bool = False) -> Report:
    import calendar
    import math
    from datetime import UTC, datetime

    y, m = (int(x) for x in month.split("-"))
    r = Report(
        symbol=symbol,
        month=month,
        bars=len(klines),
        expected_bars=_expected_bars(month),
        days_expected=calendar.monthrange(y, m)[1],
        is_first_month=is_first_month,
    )
    if not klines:
        return r
    r.days_present = len({datetime.fromtimestamp(k.open_time / 1000, UTC).day for k in klines})

    seen: set[int] = set()
    for k in klines:
        if k.open_time in seen:
            r.duplicates += 1
        seen.add(k.open_time)

        vals = (k.open, k.high, k.low, k.close, k.volume)
        if any(not math.isfinite(v) for v in vals):
            r.nonfinite += 1
            continue  # cannot compare NaN meaningfully
        # A header row parsed as data lands here, as would any corrupt row.
        if not (k.low <= k.open <= k.high and k.low <= k.close <= k.high):
            r.ohlc_violations.append(k.open_time)
        if k.volume < 0:
            r.nonpositive_volume += 1

    for a, b in pairwise(klines):
        delta = b.open_time - a.open_time
        if delta <= 0:
            r.out_of_order += 1
        elif delta > MINUTE_MS:
            r.gaps.append((a.open_time, delta // MINUTE_MS - 1))
    return r


def check_all(batches: dict[tuple[str, str], list[Kline]]) -> list[Report]:
    """Check many symbol-months. Key is (symbol, month)."""
    return [check(kl, sym, mon) for (sym, mon), kl in sorted(batches.items())]
