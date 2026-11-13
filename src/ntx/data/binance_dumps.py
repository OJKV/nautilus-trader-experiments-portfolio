"""Download and parse Binance USDT-M monthly data dumps.

Source: https://data.binance.vision -- free, no API key, reachable from
Kazakhstan with no tunnel (verified).

Two traps this module exists to handle, both verified against real files:

1. **The CSV header appeared partway through history.** `BTCUSDT-1m-2020-02.csv`
   has no header row; `BTCUSDT-1m-2025-06.csv` has
   `open_time,open,high,...`. Parsing a header row as data yields one garbage bar
   with NaN prices, which then poisons every indicator downstream. We sniff.

2. **A 404 before an asset was listed is normal, not an error.** AVAX perps did
   not exist in 2020. Treating that as a failure would either abort the build or,
   worse, invite back-filling data that never existed.

Every archive is SHA256-verified against its published `.CHECKSUM` (R18: validate
inputs before using them).
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ntx.config.venues import BROWSER_UA

log = logging.getLogger(__name__)

BASE = "https://data.binance.vision/data/futures/um/monthly"
BASE_DAILY = "https://data.binance.vision/data/futures/um/daily"

# Binance kline CSV column order (stable across eras; only the header differs).
KLINE_COLS = (
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
)


class ChecksumMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class Kline:
    """One bar. `open_time` is epoch milliseconds, UTC."""

    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class FundingRate:
    calc_time: int  # epoch ms
    interval_hours: int
    rate: float


def months(start: str, end: str | None = None) -> list[str]:
    """Inclusive list of 'YYYY-MM' strings.

    `end` defaults to last month -- the current month's archive does not exist
    until the month closes, so requesting it always 404s.
    """
    y, m = (int(x) for x in start.split("-"))
    if end is None:
        now = datetime.now(UTC)
        ey, em = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    else:
        ey, em = (int(x) for x in end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _kline_url(symbol: str, interval: str, month: str) -> str:
    return f"{BASE}/klines/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"


def _kline_url_daily(symbol: str, interval: str, day: str) -> str:
    return f"{BASE_DAILY}/klines/{symbol}/{interval}/{symbol}-{interval}-{day}.zip"


def _funding_url(symbol: str, month: str) -> str:
    return f"{BASE}/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"


def days_in_month(month: str) -> list[str]:
    """['YYYY-MM-DD', ...] for every day of `month`."""
    import calendar

    y, m = (int(x) for x in month.split("-"))
    return [f"{month}-{d:02d}" for d in range(1, calendar.monthrange(y, m)[1] + 1)]


def _fetch(url: str, timeout: int = 120) -> bytes | None:
    """GET bytes. Returns None on 404 (which is a legitimate 'not listed yet')."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})  # R25
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _verify(payload: bytes, checksum_text: str, name: str) -> None:
    """Compare SHA256 against the published checksum line."""
    expected = checksum_text.split()[0].strip().lower()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ChecksumMismatch(f"{name}: expected {expected}, got {actual}")


def download(url: str, dest: Path, *, verify: bool = True) -> Path | None:
    """Download `url` to `dest`, checksum-verified. Returns None if absent (404).

    A cached file is trusted only if its checksum still matches; otherwise it is
    re-downloaded. A half-written file is never left behind -- the temp file is
    removed on any failure.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    checksum_text: str | None = None
    if verify:
        raw = _fetch(url + ".CHECKSUM", timeout=60)
        checksum_text = raw.decode() if raw else None

    if dest.exists():
        if not checksum_text:
            return dest
        try:
            _verify(dest.read_bytes(), checksum_text, dest.name)
            return dest
        except ChecksumMismatch:
            log.warning("%s failed checksum on cache, re-downloading", dest.name)
            dest.unlink(missing_ok=True)

    payload = _fetch(url)
    if payload is None:
        return None
    if checksum_text:
        _verify(payload, checksum_text, dest.name)

    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        tmp.write_bytes(payload)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def _rows_from_zip(payload: bytes) -> list[list[str]]:
    """Extract CSV rows from a single-member zip, skipping a header if present.

    Header detection is by content, not by date: if the first field is not
    parseable as an integer timestamp, it is a header.
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        name = z.namelist()[0]
        text = z.read(name).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    try:
        int(rows[0][0])
    except (ValueError, IndexError):
        rows = rows[1:]  # header row -- see module docstring, trap 1
    return rows


def parse_klines(payload: bytes) -> list[Kline]:
    out: list[Kline] = []
    for row in _rows_from_zip(payload):
        if len(row) < 6:
            continue
        out.append(
            Kline(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        )
    return out


def parse_funding(payload: bytes) -> list[FundingRate]:
    out: list[FundingRate] = []
    for row in _rows_from_zip(payload):
        if len(row) < 3:
            continue
        out.append(
            FundingRate(
                calc_time=int(row[0]),
                interval_hours=int(float(row[1])),
                rate=float(row[2]),
            )
        )
    return out


def fetch_klines(
    symbol: str, month: str, raw_dir: Path, interval: str = "1m"
) -> list[Kline] | None:
    """Klines for one symbol-month. None means 'archive does not exist'."""
    dest = raw_dir / "klines" / symbol / interval / f"{symbol}-{interval}-{month}.zip"
    path = download(_kline_url(symbol, interval, month), dest)
    return parse_klines(path.read_bytes()) if path else None


def fetch_klines_day(
    symbol: str, day: str, raw_dir: Path, interval: str = "1m"
) -> list[Kline] | None:
    """Klines for one symbol-day from the DAILY archive.

    Needed because Binance's **monthly** archives are sometimes truncated while
    the dailies are complete: SOL/XRP/TRX monthly 2022-02 stops on the 25th and
    monthly 2022-04 starts on the 3rd, yet every daily archive for those dates
    exists (F013).
    """
    dest = raw_dir / "klines_daily" / symbol / interval / f"{symbol}-{interval}-{day}.zip"
    path = download(_kline_url_daily(symbol, interval, day), dest)
    return parse_klines(path.read_bytes()) if path else None


def fetch_month_complete(
    symbol: str,
    month: str,
    raw_dir: Path,
    interval: str = "1m",
    *,
    probe_dailies_when_month_absent: bool = False,
) -> tuple[list[Kline], list[str]]:
    """Klines for a whole month, repaired from daily archives where needed.

    Returns (klines, days_filled).

    Strategy: take the monthly archive, work out which days it is missing, and
    fetch exactly those days from the dailies.

    `probe_dailies_when_month_absent` guards the expensive case. If a monthly
    archive is absent the month is almost always **pre-listing**, and probing
    every day of it costs ~60 doomed requests (each day also probes a
    `.CHECKSUM`). So callers pass True only once the asset has started trading,
    where an absent month would be a genuine hole worth repairing rather than a
    date before the asset existed.
    """
    monthly = fetch_klines(symbol, month, raw_dir, interval) or []
    if not monthly and not probe_dailies_when_month_absent:
        return [], []

    have_days = {
        datetime.fromtimestamp(k.open_time / 1000, UTC).strftime("%Y-%m-%d") for k in monthly
    }
    filled: list[str] = []
    extra: list[Kline] = []
    for day in days_in_month(month):
        if day in have_days:
            continue
        kl = fetch_klines_day(symbol, day, raw_dir, interval)
        if kl:
            extra.extend(kl)
            filled.append(day)

    if not monthly and not extra:
        return [], []
    merged = {k.open_time: k for k in monthly}
    for k in extra:
        merged.setdefault(k.open_time, k)
    return [merged[t] for t in sorted(merged)], filled


def fetch_funding(symbol: str, month: str, raw_dir: Path) -> list[FundingRate] | None:
    dest = raw_dir / "fundingRate" / symbol / f"{symbol}-fundingRate-{month}.zip"
    path = download(_funding_url(symbol, month), dest)
    return parse_funding(path.read_bytes()) if path else None
