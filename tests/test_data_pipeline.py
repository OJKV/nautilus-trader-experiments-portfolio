"""Phase 1 data-pipeline tests.

Real archives are never downloaded here -- every test is offline and synthetic,
except those marked `live` (excluded by default).
"""

from __future__ import annotations

import csv
import io
import zipfile

import pytest

from ntx.config.universe import BY_BASE, BY_BINANCE, BY_OKX, UNIVERSE
from ntx.data.aggregate import aggregate
from ntx.data.binance_dumps import Kline, months, parse_funding, parse_klines
from ntx.data.integrity import check

MIN = 60_000


def _zip_csv(rows: list[list], header: list[str] | None = None) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    if header:
        w.writerow(header)
    w.writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("data.csv", buf.getvalue())
    return out.getvalue()


def _kline_row(i: int, o=100.0, h=101.0, low=99.0, c=100.5, v=1.0) -> list:
    return [i * MIN, o, h, low, c, v, i * MIN + MIN - 1, 0, 0, 0, 0, 0]


def _series(n: int, *, start: int = 0, step_min: int = 1) -> list[Kline]:
    return [
        Kline(
            open_time=(start + i * step_min) * MIN,
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=1.0,
        )
        for i in range(n)
    ]


# --------------------------------- universe --------------------------------- #
def test_universe_maps_are_consistent():
    assert len(UNIVERSE) == 10
    assert len(BY_BINANCE) == len(BY_OKX) == len(BY_BASE) == 10
    for a in UNIVERSE:
        assert a.binance_symbol.endswith("USDT")
        assert a.okx_inst_id.endswith("-USDT-SWAP")
        assert a.okx_inst_id.startswith(a.base)


# ------------------------------ month arithmetic ---------------------------- #
def test_months_inclusive_and_crosses_year_boundary():
    assert months("2020-11", "2021-02") == ["2020-11", "2020-12", "2021-01", "2021-02"]
    assert months("2024-03", "2024-03") == ["2024-03"]


def test_months_auto_end_excludes_current_month():
    """The current month's archive does not exist until the month closes."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    assert f"{now.year:04d}-{now.month:02d}" not in months("2024-01")


# ------------------------------- CSV parsing -------------------------------- #
def test_parses_headerless_csv_2020_era():
    payload = _zip_csv([_kline_row(i) for i in range(3)])
    assert len(parse_klines(payload)) == 3


def test_parses_headered_csv_2025_era():
    """Binance added a header row partway through history; a header parsed as
    data becomes a garbage bar with NaN prices."""
    header = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
        "count",
        "taker_buy_volume",
        "taker_buy_quote_volume",
        "ignore",
    ]
    payload = _zip_csv([_kline_row(i) for i in range(3)], header=header)
    kl = parse_klines(payload)
    assert len(kl) == 3, "header row must be skipped, not parsed as a bar"
    assert kl[0].open_time == 0


def test_funding_csv_has_header():
    payload = _zip_csv(
        [[1704067200000, 8, 0.00037409], [1704096000000, 8, 0.00027213]],
        header=["calc_time", "funding_interval_hours", "last_funding_rate"],
    )
    fr = parse_funding(payload)
    assert len(fr) == 2
    assert fr[0].interval_hours == 8
    assert fr[0].rate == pytest.approx(0.00037409)


def test_empty_archive_yields_no_bars():
    assert parse_klines(_zip_csv([])) == []


# -------------------------------- aggregation ------------------------------- #
def test_aggregate_1h_is_exact():
    res = aggregate(_series(120), "1h")
    assert len(res.bars) == 2
    assert res.incomplete_buckets == 0
    b = res.bars[0]
    assert b.open == 100.0  # first open
    assert b.high == 101.0 + 59  # max high
    assert b.low == 99.0  # min low
    assert b.close == 100.5 + 59  # last close
    assert b.volume == 60.0  # summed


def test_aggregate_buckets_align_to_utc_epoch():
    """4h bars must start at 00:00/04:00/... UTC -- funding settles on 8h UTC
    boundaries, and misaligned buckets would smear it across bars."""
    res = aggregate(_series(240, start=150), "4h")
    for b in res.bars:
        assert b.open_time % (4 * 3600_000) == 0


def test_aggregate_reports_incomplete_buckets_without_inventing_bars():
    res = aggregate(_series(30), "1h")  # half an hour of data
    assert len(res.bars) == 1
    assert res.incomplete_buckets == 1
    assert res.minutes_missing == 30
    assert res.bars[0].volume == 30.0  # only real minutes counted


def test_aggregate_1m_is_identity():
    src = _series(10)
    assert aggregate(src, "1m").bars == src


def test_aggregate_rejects_unknown_timeframe():
    with pytest.raises(ValueError, match="unsupported timeframe"):
        aggregate(_series(10), "7m")


# --------------------------------- integrity -------------------------------- #
def _full_month(month: str, *, drop: set[int] | None = None) -> list[Kline]:
    """A complete month of 1m klines, optionally dropping minute indices.

    Full months matter now that a truncated month is fatal (F013) -- a short
    synthetic series would fail on day coverage rather than on what is under
    test.
    """
    import calendar
    from datetime import UTC, datetime

    y, m = (int(x) for x in month.split("-"))
    start_ms = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
    total = calendar.monthrange(y, m)[1] * 1440
    drop = drop or set()
    return [
        Kline(
            open_time=start_ms + i * MIN,
            open=100.0 + i % 7,
            high=101.0 + i % 7,
            low=99.0 + i % 7,
            close=100.5 + i % 7,
            volume=1.0,
        )
        for i in range(total)
        if i not in drop
    ]


def test_clean_series_passes():
    r = check(_full_month("2024-01"), "BTCUSDT", "2024-01")
    assert r.ok
    assert r.duplicates == 0 and not r.gaps and not r.ohlc_violations
    assert r.missing_days == 0


def test_duplicates_detected():
    kl = _series(10)
    r = check([*kl, kl[5]], "BTCUSDT", "2024-01")
    assert r.duplicates == 1
    assert not r.ok


def test_gap_detected_and_measured():
    kl = _series(5) + _series(5, start=65)  # 60 missing minutes
    r = check(kl, "BTCUSDT", "2024-01")
    assert len(r.gaps) == 1
    assert r.largest_gap_minutes == 60
    assert not r.ok, "an hour-long gap must be fatal: a 1h bar across it is wrong"


def test_small_gap_tolerated():
    """A few missing minutes is a real Binance outage, not a corrupt archive."""
    kl = _full_month("2024-01", drop={5000, 5001, 5002})
    r = check(kl, "BTCUSDT", "2024-01")
    assert r.largest_gap_minutes == 3
    assert r.missing_days == 0, "the day is still present, just short a few minutes"
    assert r.ok, "short Binance outages are real and tolerable"


def test_ohlc_violation_detected():
    bad = Kline(open_time=0, open=100.0, high=99.0, low=101.0, close=100.0, volume=1.0)
    r = check([bad], "BTCUSDT", "2024-01")
    assert r.ohlc_violations == [0]
    assert not r.ok


def test_nan_row_detected_not_crashed():
    """A header row that slipped through would surface as NaN, not an exception."""
    nan = float("nan")
    r = check(
        [Kline(open_time=0, open=nan, high=nan, low=nan, close=nan, volume=nan)],
        "BTCUSDT",
        "2024-01",
    )
    assert r.nonfinite == 1
    assert not r.ok


def test_out_of_order_detected():
    kl = _series(5)
    r = check([kl[0], kl[3], kl[1]], "BTCUSDT", "2024-01")
    assert r.out_of_order >= 1
    assert not r.ok


def test_expected_bar_count_is_month_length():
    assert check([], "BTCUSDT", "2024-02").expected_bars == 29 * 1440  # leap year
    assert check([], "BTCUSDT", "2023-02").expected_bars == 28 * 1440
    assert check([], "BTCUSDT", "2024-01").expected_bars == 31 * 1440


# --------------------- catalog round-trip (regression guard) ---------------- #
def test_bar_type_filter_actually_filters(tmp_path):
    """Regression guard for the bug that `query(**kwargs)` hides.

    Calling `query(data_cls=Bar, bar_types=[...])` -- not a real parameter --
    silently returned every bar for every symbol and timeframe. `read_bars`
    must return exactly one instrument at exactly one timeframe, or raise.
    """
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    from ntx.data.catalog import bar_type_for, to_bars
    from ntx.data.instruments import build_instrument
    from ntx.data.read import available_bar_types, read_bars

    specs = {
        "SOL-USDT-SWAP": dict(
            instId="SOL-USDT-SWAP",
            ctVal="1",
            ctValCcy="SOL",
            minSz="0.01",
            lotSz="0.01",
            tickSz="0.01",
            lever="100",
        ),
        "BTC-USDT-SWAP": dict(
            instId="BTC-USDT-SWAP",
            ctVal="0.01",
            ctValCcy="BTC",
            minSz="0.01",
            lotSz="0.01",
            tickSz="0.1",
            lever="100",
        ),
    }
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True)
    catalog = ParquetDataCatalog(str(catalog_dir))

    for iid, spec in specs.items():
        inst = build_instrument(spec)
        catalog.write_data([inst])
        base = 100.0 if iid.startswith("SOL") else 40000.0
        src = [
            Kline(
                open_time=i * MIN,
                open=base,
                high=base + 1,
                low=base - 1,
                close=base + 0.5,
                volume=1.0,
            )
            for i in range(240)
        ]
        for tf in ("1h", "4h"):
            res = aggregate(src, tf)
            catalog.write_data(to_bars(res.bars, inst, bar_type_for(inst, tf)))

    assert len(available_bar_types(tmp_path)) == 4

    sol_1h = read_bars(catalog, "SOL-USDT-SWAP.OKX", "1h")
    assert len(sol_1h) == 4, "240 minutes -> four 1h bars, not everything in the catalog"
    assert {str(b.bar_type) for b in sol_1h} == {"SOL-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL"}
    assert all(99.0 <= float(b.close) <= 101.0 for b in sol_1h), "must not be BTC prices"

    btc_4h = read_bars(catalog, "BTC-USDT-SWAP.OKX", "4h")
    assert len(btc_4h) == 1
    assert float(btc_4h[0].close) > 10_000


def test_read_bars_rejects_unknown_instrument(tmp_path):
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    from ntx.data.read import read_bars

    d = tmp_path / "catalog"
    d.mkdir(parents=True)
    with pytest.raises(KeyError):
        read_bars(ParquetDataCatalog(str(d)), "NOPE-USDT-SWAP.OKX", "1h")


def test_open_catalog_requires_existing_path(tmp_path):
    from ntx.data.read import open_catalog

    with pytest.raises(FileNotFoundError, match="make data"):
        open_catalog(tmp_path / "missing")


def test_instrument_precision_comes_from_okx_spec():
    """Backtest precision must match the venue we trade, not the data source."""
    from ntx.data.instruments import build_instrument, min_notional_usd

    inst = build_instrument(
        dict(
            instId="BTC-USDT-SWAP",
            ctVal="0.01",
            ctValCcy="BTC",
            minSz="0.01",
            lotSz="0.01",
            tickSz="0.1",
            lever="100",
        )
    )
    assert inst.price_precision == 1  # tickSz 0.1
    assert inst.size_precision == 2  # lotSz 0.01
    assert str(inst.multiplier) == "0.01"  # ctVal: contracts -> base units
    assert min_notional_usd(inst, 76_700.0) == pytest.approx(7.67, abs=0.01)


# ------------------- catalog month coverage (F011 regression) --------------- #
def test_month_span_ends_at_last_bucket_not_month_end():
    """The catalog records intervals by first/last BAR timestamp.

    A complete March of 15m bars ends 23:45 on the 31st, 1h at 23:00, 4h at
    20:00. Comparing against month-end instead made every coverage check fail,
    so the builder re-wrote months that already existed and hit
    'non-disjoint intervals' (F011).
    """
    import datetime as dt

    from ntx.data.catalog import _month_span_ns

    def as_utc(ns: int) -> dt.datetime:
        return dt.datetime.fromtimestamp(ns / 1e9, dt.UTC)

    for tf, expect in (
        ("15m", "2024-03-31 23:45"),
        ("1h", "2024-03-31 23:00"),
        ("4h", "2024-03-31 20:00"),
    ):
        start, last = _month_span_ns("2024-03", tf)
        assert as_utc(start).strftime("%Y-%m-%d %H:%M") == "2024-03-01 00:00"
        assert as_utc(last).strftime("%Y-%m-%d %H:%M") == expect


def test_month_span_handles_february_leap_year():
    import datetime as dt

    from ntx.data.catalog import _month_span_ns

    _, last = _month_span_ns("2024-02", "1h")
    assert dt.datetime.fromtimestamp(last / 1e9, dt.UTC).day == 29
    _, last = _month_span_ns("2023-02", "1h")
    assert dt.datetime.fromtimestamp(last / 1e9, dt.UTC).day == 28


def test_covered_detects_cached_month():
    from ntx.data.catalog import _covered, _month_span_ns

    start, last = _month_span_ns("2024-02", "1h")
    assert _covered([(start, last)], "2024-02", "1h")
    # A wider interval (a Q1 file) also covers February.
    q1_start, _ = _month_span_ns("2024-01", "1h")
    _, q1_last = _month_span_ns("2024-03", "1h")
    assert _covered([(q1_start, q1_last)], "2024-02", "1h")


def test_covered_rejects_partial_and_absent_months():
    from ntx.data.catalog import _covered, _month_span_ns

    start, last = _month_span_ns("2024-02", "1h")
    assert not _covered([], "2024-02", "1h")
    assert not _covered([(start, last - 3_600_000_000_000)], "2024-02", "1h"), "short by one bar"
    assert not _covered([(start + 3_600_000_000_000, last)], "2024-02", "1h"), "starts late"
    # A different month must not count as coverage.
    mar_s, mar_l = _month_span_ns("2024-03", "1h")
    assert not _covered([(mar_s, mar_l)], "2024-02", "1h")


def test_month_boundaries_never_split_a_bucket():
    """Per-month aggregation is only exact because 15m/1h/4h divide a day evenly
    and months start at 00:00 UTC."""
    from ntx.data.aggregate import TF_MINUTES

    for tf in ("15m", "1h", "4h"):
        assert 1440 % TF_MINUTES[tf] == 0, f"{tf} does not divide a day evenly"


# ---------------- truncated months (F013 regression) ----------------------- #
def test_truncated_month_is_fatal():
    """F013: a month that simply STOPS early has no internal gap, so gap
    detection cannot see it. Three assets silently lost 5 days each that way."""
    # 25 of 28 days of February, then nothing.
    kl = _series(25 * 1440)
    r = check(kl, "XRPUSDT", "2022-02")
    assert r.missing_days == 3
    assert not r.ok, "a truncated month must fail integrity"


def test_truncated_first_month_is_allowed():
    """A late start in the asset's first month is its listing date, not a defect."""
    kl = _series(16 * 1440)
    r = check(kl, "SOLUSDT", "2020-09", is_first_month=True)
    assert r.missing_days > 0
    assert r.ok, "the listing month is legitimately partial"


def test_complete_month_reports_no_missing_days():
    r = check(_series(28 * 1440), "XRPUSDT", "2022-02")
    assert r.missing_days == 0
    assert r.ok


def test_days_in_month():
    from ntx.data.binance_dumps import days_in_month

    assert len(days_in_month("2022-02")) == 28
    assert len(days_in_month("2024-02")) == 29
    assert days_in_month("2022-04")[0] == "2022-04-01"
    assert days_in_month("2022-04")[-1] == "2022-04-30"


def test_month_absent_does_not_probe_dailies_by_default(monkeypatch):
    """Guard against the inefficiency introduced with the fix: probing every day
    of a pre-listing month costs ~60 doomed requests per month."""
    from ntx.data import binance_dumps as bd

    calls: list[str] = []
    monkeypatch.setattr(bd, "fetch_klines", lambda *a, **k: None)
    monkeypatch.setattr(bd, "fetch_klines_day", lambda s, day, *a, **k: calls.append(day) or None)

    import pathlib

    kl, filled = bd.fetch_month_complete("SOLUSDT", "2020-03", pathlib.Path("/tmp"))
    assert kl == [] and filled == []
    assert calls == [], "must not probe dailies for a pre-listing month"


def test_month_absent_probes_dailies_when_asked(monkeypatch):
    """After the asset has started trading, an absent month IS a hole worth
    repairing from the dailies."""
    import pathlib

    from ntx.data import binance_dumps as bd

    calls: list[str] = []

    def fake_day(symbol, day, raw_dir, interval="1m"):
        calls.append(day)
        return [Kline(open_time=0, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]

    monkeypatch.setattr(bd, "fetch_klines", lambda *a, **k: None)
    monkeypatch.setattr(bd, "fetch_klines_day", fake_day)

    kl, filled = bd.fetch_month_complete(
        "SOLUSDT",
        "2022-04",
        pathlib.Path("/tmp"),
        probe_dailies_when_month_absent=True,
    )
    assert len(calls) == 30, "April has 30 days"
    assert len(filled) == 30
    assert kl, "repaired klines must be returned"


def test_partial_month_fills_only_missing_days(monkeypatch):
    import pathlib

    from ntx.data import binance_dumps as bd

    MIN_MS = 60_000
    day_ms = 1440 * MIN_MS
    # Monthly archive covers 2022-04-03 onward only (first 2 days missing).
    base = int(
        __import__("datetime").datetime(2022, 4, 1, tzinfo=__import__("datetime").UTC).timestamp()
        * 1000
    )
    monthly = [
        Kline(
            open_time=base + 2 * day_ms + i * MIN_MS,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
        )
        for i in range(28 * 1440)
    ]
    asked: list[str] = []

    def fake_day(symbol, day, raw_dir, interval="1m"):
        asked.append(day)
        return [Kline(open_time=base, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]

    monkeypatch.setattr(bd, "fetch_klines", lambda *a, **k: monthly)
    monkeypatch.setattr(bd, "fetch_klines_day", fake_day)

    _, filled = bd.fetch_month_complete("XRPUSDT", "2022-04", pathlib.Path("/tmp"))
    assert asked == ["2022-04-01", "2022-04-02"], f"only missing days, got {asked}"
    assert filled == ["2022-04-01", "2022-04-02"]


# ------------------ bar timestamps (F014 lookahead regression) -------------- #
def test_bar_ts_event_is_close_time():
    """F014: `ts_event` must be the bar's CLOSE time, not its open.

    The engine delivers a bar at its `ts_event` and lets the strategy act there.
    Stamped with the open time, a strategy received the 02:00-03:00 bar at 02:00
    and could read its close before it happened -- direct lookahead bias, which
    silently invalidated a 146-trade backtest.
    """
    import datetime as dt

    from ntx.data.catalog import bar_type_for, to_bars
    from ntx.data.instruments import build_instrument

    inst = build_instrument(
        dict(
            instId="SOL-USDT-SWAP",
            ctVal="1",
            ctValCcy="SOL",
            minSz="0.01",
            lotSz="0.01",
            tickSz="0.01",
            lever="100",
        )
    )
    open_ms = int(dt.datetime(2024, 1, 1, tzinfo=dt.UTC).timestamp() * 1000)
    kl = [Kline(open_time=open_ms, open=100.0, high=101.0, low=99.0, close=100.5, volume=1.0)]

    for tf, minutes in (("15m", 15), ("1h", 60), ("4h", 240)):
        bar = to_bars(kl, inst, bar_type_for(inst, tf))[0]
        expected = (open_ms + minutes * 60_000) * 1_000_000
        assert bar.ts_event == expected, f"{tf}: ts_event is not the close time"
        assert bar.ts_init == expected


def test_bar_period_derived_from_bar_type():
    """The period comes from the BarType itself, so a bar cannot be stamped with
    another timeframe's close time."""
    from ntx.data.catalog import _bar_period_ns, bar_type_for
    from ntx.data.instruments import build_instrument

    inst = build_instrument(
        dict(
            instId="SOL-USDT-SWAP",
            ctVal="1",
            ctValCcy="SOL",
            minSz="0.01",
            lotSz="0.01",
            tickSz="0.01",
            lever="100",
        )
    )
    assert _bar_period_ns(bar_type_for(inst, "15m")) == 15 * 60 * 1_000_000_000
    assert _bar_period_ns(bar_type_for(inst, "1h")) == 3_600_000_000_000
    assert _bar_period_ns(bar_type_for(inst, "4h")) == 4 * 3_600_000_000_000
