"""Aggregate 1-minute klines into higher timeframes.

Exact aggregation only -- no interpolation, no forward-fill. A bucket with
missing minutes is aggregated from what exists and reported, never invented.
Fabricated bars are indistinguishable from real ones downstream, which is how a
backtest quietly becomes fiction.

Buckets are aligned to the UTC epoch, so a 4h bar always starts at
00:00/04:00/... UTC. This matters: OKX funding settles on 8h UTC boundaries, and
misaligned buckets would smear funding across bars.
"""

from __future__ import annotations

from dataclasses import dataclass

from ntx.data.binance_dumps import Kline

TF_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "1d": 1440,
}


@dataclass(frozen=True)
class AggregateResult:
    bars: list[Kline]
    incomplete_buckets: int  # buckets with fewer 1m bars than expected
    minutes_missing: int


def aggregate(klines: list[Kline], timeframe: str) -> AggregateResult:
    """Resample 1m klines to `timeframe`.

    O = first open, H = max high, L = min low, C = last close, V = sum volume.
    Input must be sorted ascending; callers get that from `integrity.check`
    which reports `out_of_order`.
    """
    if timeframe not in TF_MINUTES:
        raise ValueError(f"unsupported timeframe {timeframe!r}; known: {sorted(TF_MINUTES)}")
    step = TF_MINUTES[timeframe]
    if step == 1:
        return AggregateResult(list(klines), 0, 0)
    bucket_ms = step * 60_000

    buckets: dict[int, list[Kline]] = {}
    for k in klines:
        buckets.setdefault(k.open_time - (k.open_time % bucket_ms), []).append(k)

    bars: list[Kline] = []
    incomplete = 0
    missing = 0
    for start in sorted(buckets):
        group = buckets[start]
        if len(group) < step:
            incomplete += 1
            missing += step - len(group)
        bars.append(
            Kline(
                open_time=start,
                open=group[0].open,
                high=max(g.high for g in group),
                low=min(g.low for g in group),
                close=group[-1].close,
                volume=sum(g.volume for g in group),
            )
        )
    return AggregateResult(bars, incomplete, missing)
