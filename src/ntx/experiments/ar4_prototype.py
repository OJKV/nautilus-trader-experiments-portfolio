"""AR-4 P0 desk study: cross-venue perpetual funding differential (= XA-1 / FC-2).
Test 3 of `docs/strategies/research/arbitrage.md` section 6. IS ONLY by default (R37).

    python -m ntx.experiments.ar4_prototype                  # downloads on first run, then IS
    python -m ntx.experiments.ar4_prototype --oos            # ONCE, only IS-passing theta
    python -m ntx.experiments.ar4_prototype --pair=okx       # one pair only (bybit | okx)

Free data only, public endpoints, nothing live, no keys:
    Binance   funding + 1m klines on disk (data/raw), 1h closes from the 1m dumps (sa1 `hourly`)
    Bybit     v5/market/funding/history and v5/market/kline (interval 60), cached to
              data/raw/bybit/{funding,kline1h}/<SYM>.csv (browser UA, R25)
    OKX       the historical-data CDN files, fetched through the reachable host my.okx.com
              (`/cdn/okex/traderecords/swaprates/monthly/YYYYMM/<BASE>-USDT-SWAP-fundingrates-
              YYYY-MM.zip` and `.../candlesticks/monthly/...-candlesticks-YYYY-MM.zip`, 1m bars
              folded to 1h on download). static.okx.com times out from here; www.okx.com is
              blocked. Cached under data/raw/bybit/okx/ (the only directory this task may write).
              OKX monthly files are SINGAPORE-time months (UTC+8): rows are absolute epoch ms, so
              months are concatenated and de-duplicated, never re-labelled.

DATA FACTS MEASURED BEFORE THE FIRST RUN (R11; nothing here changes the declaration):
  OKX        file listing API `my.okx.com/api/v5/public/market-data-history?module=3` (module 3 =
             funding rate, 2 = 1m candlesticks) works but throttles (50011) and rejects long
             ranges (50077), so the monthly URLs are constructed from the pattern. Probed
             2022-03..2026-09 for all ten symbols (1,100 files). Every file validated: zip magic,
             header, instrument name (an HTML 200 would count as missing). MISSING, recorded not
             skipped silently: BNB-USDT-SWAP 2022-03..2022-11 (18 files; OKX history for BNB
             starts 2022-12-23, so BNB enters the okx pair late). Funding rows carry seconds of
             jitter (rounded to the hour) and one irregular pair of rows on 2022-12-18 (08:00,
             18:54, 00:00; the 10.9 h / 5.1 h spacings give NaN per-hour rates by design). AVAX
             had 1 h funding spacing for 89 rows. Prices: OKX 1m folded to 1h (a bar needs its
             :59 minute, else NaN); 2 to 4 gaps over 1 h per symbol.
  Bybit      funding rows from 2020-03 (BTC), 2020-10 (ETH, LINK), 2021 (rest); SOL moved to 2 h
             spacing for 480 rows. 1h klines start LATER than funding (ETH 2021-03-15, SOL
             2021-10-15, LINK 2020-10-21, BTC 2020-03-25): the venue returns nothing earlier
             (probed with `end=` before those dates), so the price leg limits the effective IS
             start per symbol. Spot check: mean |Bybit/Binance - 1| 2.5 bps and |OKX/Binance - 1|
             3.8 bps on hourly closes in 2022-03 (a level offset; only changes enter the P&L).

DECLARATION (arbitrage.md section 6, Test 3) AND THE CLARIFICATIONS MADE BEFORE THE FIRST RUN
(R10: the grid and rule are fixed; every clarification below is the most conservative reading
and none changes a number the declaration states):

  pairs      TWO runs, reported SEPARATELY, never pooled:
             bybit = Binance (A) vs Bybit (B), "structure proxy": shows whether differentials
                     exist and persist at all, NOT that OKX has them. IS 2020-01..2023-12, but
                     Bybit history starts ~2020-12 (per symbol later), so the effective IS is
                     shorter; the per-symbol start is printed.
             okx   = Binance (A) vs OKX (B), the decision-relevant run, available because one
                     OKX file downloaded cleanly (R11). IS 2022-03-01..2023-12-31 (shorter, as
                     the declaration says). OOS 2024-01..2026-08 for both pairs.
             A pair whose symbol has zero data in the window raises (R35), never "0 trades".
  universe   BTC ETH SOL BNB XRP DOGE ADA LINK AVAX TRX (USDT-margined perps), as fc1.
  rates      each venue's rate is normalised to PER HOUR at each settlement as
             rate / (spacing in hours to that venue's previous settlement). Only spacings of
             1, 2, 4 or 8 h are accepted; anything else (a missing row) and the first row of a
             series give NaN, never a silently halved rate. The per-hour rate is held forward
             on an hourly grid from the LAST settlement at or before t (a rate older than 8 h
             is NaN), so nothing after t is ever used.
  signal     s_t = (rate_A - rate_B) * 8 in bps per 8h-equivalent (per-hour rates * 8 * 1e4).
             s_hat_t = mean of the last 72 hourly s values (window (t-72h, t], all 72 required).
             Decision times = every hour that is a settlement on either venue and where both
             1h closes exist. Entry fills at that hour's close (R8); the settlement at the
             entry hour is NOT received (conservative: the position starts after it).
  entry      |s_hat| >= theta AND |s_t| >= theta, theta in {1, 2} bps per 8h, AND the two have
             the SAME sign (the declaration does not say what to do if they disagree; no trade
             is the conservative reading). pos=+1 (s>0): SHORT A / LONG B; pos=-1 the reverse.
             Equal notional. One position per symbol; a new entry is considered from the first
             decision time AFTER an exit.
  exit       at the first decision time where |s_hat| < theta/2, or s_hat is NaN (a data gap:
             never hold blind), or the position is >= 14 days old. A flip of sign with
             |s_hat| >= theta/2 does NOT exit (verbatim). A position still open when the data
             end is dropped and counted (`open_at_end`); no mark-to-market.
  P&L        bps of ONE leg's notional. funding = pos * (sum rate_A - sum rate_B) over each
             venue's OWN settlements strictly after entry and at-or-before exit, using the raw
             settled rates (never rate/hour * hours). price = pos * ((pB1/pB0-1) - (pA1/pA0-1))
             on 1h closes (the move in the price difference). net = funding + price - cost.
  costs      four perp fills per round trip, bps of one leg's notional (as sa1): maker 8,
             taker 20, taker-plus-slippage 40; each also at 2x (2x doubles slippage too, not
             funding). Gates (1), (2), (4), (5) are judged at maker-2x = 16 bps (the declared
             gate level); the taker levels are reported only.
  controls   (a) RANDOM: each real trade is replaced by a trade on the same symbol at a
             uniformly random decision time with a random side and the SAME hold time (exit =
             first decision time >= entry + hold), 20 replications, seed 20261105; (b) STATIC:
             the real entry and exit times, but the side is "short the venue with the higher
             trailing-30-day mean funding" (720 h mean of s, past only). Trades whose 30-day
             mean is undefined or zero are excluded and the strategy mean is compared on the
             SAME subset. "Beats" means a strictly higher maker-2x mean.
  PASS (IS)  ALL of: (1) maker-2x mean > 0 and t >= 2 (naive, trades pooled); (2) 2023 alone
             mean > 0 with >= 30 trades (entries in 2023); (3) >= 50 trades in 2023; (4) mean
             > 0 after dropping every trade whose hold overlaps a top-1% day or the FTX window
             2022-11-06..2022-11-20 (a "day" = one symbol-UTC-day, scored by its max |s| over
             the evaluated window, pooled over symbols; top 1% = at or above the 99th pct);
             (5) beats both controls.
  PASS (OOS) for each IS-passing theta only, once: (1), (4), (5) again on OOS, and (3) read as
             >= 50 trades per year on average over the OOS window AND >= 50 in every COMPLETE
             calendar year in it (2024, 2025). (2) is an IS-year gate and is not re-applied.
  verdict    per pair: IS = PASS if both theta pass, BORDERLINE if one, FAIL if none (r35
             style). OOS runs only for IS-passing theta; FINAL = PASS only if every theta
             passes IS and OOS, BORDERLINE if at least one passes both, else FAIL. A Bybit
             pass never stands in for OKX.
  capital    reported, not a gate (arbitrage.md "gate C"): posted = 2 legs * (1/3 margin) =
             2/3 of one-leg notional; return on posted = net / (2/3); excess = return on
             posted - 4% * hold_days / 365, per trade; annualised = sum of returns on posted /
             sum of hold-years (capital deployed only).
  kill       any fail and XA-1 / FC-2 are archived; theta is not retuned (R10).
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from ntx.backtest.manifest import record
from ntx.experiments._common import IS_WINDOW, OOS_WINDOW
from ntx.experiments.fc1_prototype import SYMBOLS
from ntx.experiments.sa1_prototype import funding as binance_funding
from ntx.experiments.sa1_prototype import hourly as binance_hourly

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "data" / "raw"
CACHE = RAW / "bybit"
BIN_KLINES = RAW / "klines"

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
BYBIT = "https://api.bybit.com/v5/market"
OKX_CDN = "https://my.okx.com/cdn/okex/traderecords"

THETAS = (1.0, 2.0)  # bps per 8 h
FORECAST_H = 72
STATIC_H = 720
MAX_HOLD_H = 14 * 24
VALID_SPACING_H = (1.0, 2.0, 4.0, 8.0)
MAX_AGE_H = 8.0
COST_BPS = {"maker": 8.0, "taker": 20.0, "taker+slip": 40.0}  # 1x, four fills
GATE_COST = ("maker", 2.0)
POSTED_FRAC = 2.0 / 3.0
BENCH = 0.04
FTX = ("2022-11-06", "2022-11-20 23:59:59")
SEED = 20261105
REPS = 20
MIN_TRADES_2023 = 30
MIN_TRADES_YEAR = 50

PAIRS = {
    "bybit": {"venue": "bybit", "is": IS_WINDOW, "oos": OOS_WINDOW},
    "okx": {"venue": "okx", "is": ("2022-03-01", IS_WINDOW[1]), "oos": OOS_WINDOW},
}
FETCH_START = "2020-01-01"
FETCH_END = "2026-09-30"
BINANCE_LAST = "2026-08-31"  # last Binance month on disk


def to_sec(idx: pd.DatetimeIndex) -> np.ndarray:
    """Epoch seconds, independent of the index's datetime unit (pandas 3 varies it)."""
    return np.asarray((idx - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1), dtype="int64")


# ---------------------------------------------------------------- pure arithmetic


def per_hour(f: pd.Series) -> pd.Series:
    """Raw settled rates (indexed by settlement time) -> per-hour rates.

    rate / spacing to the previous settlement on the same venue; spacing must be 1, 2, 4 or 8
    hours, otherwise NaN (a missing row would otherwise halve the rate). First row is NaN.
    """
    ts = pd.Series(f.index, index=f.index)
    gap = ts.diff().dt.total_seconds() / 3600.0
    ok = gap.isin(VALID_SPACING_H)
    return (f / gap).where(ok)


def step_hourly(ph: pd.Series, grid: pd.DatetimeIndex, max_age_h: float = MAX_AGE_H) -> np.ndarray:
    """Per-hour rate of the LAST settlement at or before each grid time (NaN if older than
    `max_age_h`, before the first settlement, or itself NaN). Never looks forward."""
    ts, g = to_sec(ph.index), to_sec(grid)
    pos = np.searchsorted(ts, g, side="right") - 1
    ok = pos >= 0
    posc = np.clip(pos, 0, max(len(ts) - 1, 0))
    age_h = (g - ts[posc]) / 3600.0
    out = ph.to_numpy(dtype=float)[posc]
    return np.where(ok & (age_h <= max_age_h), out, np.nan)


def cost_bps(level: str, mult: float) -> float:
    return COST_BPS[level] * mult


@dataclass
class Pair:
    sym: str
    grid: pd.DatetimeIndex
    sec: np.ndarray  # grid in epoch seconds
    pa: np.ndarray
    pb: np.ndarray
    s: np.ndarray  # bps per 8h-equivalent
    shat: np.ndarray
    m30: np.ndarray
    ts_a: np.ndarray  # raw settlement times (epoch s) and cumulative raw rates
    cum_a: np.ndarray
    ts_b: np.ndarray
    cum_b: np.ndarray
    dec: np.ndarray  # grid positions of decision times


def make_pair(sym: str, fa: pd.Series, fb: pd.Series, pa: pd.Series, pb: pd.Series) -> Pair:
    """Assemble one symbol-pair from raw settled rates (indexed by settlement time) and 1h
    closes (indexed by CLOSE time). Every series is cut to the overlap of the two venues."""
    for name, x in (("fa", fa), ("fb", fb), ("pa", pa), ("pb", pb)):
        if x is None or len(x) == 0:
            raise RuntimeError(f"{sym}: no {name} data (R35)")
    lo = max(pa.index[0], pb.index[0], fa.index[0], fb.index[0])
    hi = min(pa.index[-1], pb.index[-1], fa.index[-1], fb.index[-1])
    if not lo < hi:
        raise RuntimeError(f"{sym}: venues do not overlap (R35)")
    grid = pd.date_range(lo.ceil("h"), hi.floor("h"), freq="h", tz="UTC")
    ha, hb = step_hourly(per_hour(fa), grid), step_hourly(per_hour(fb), grid)
    s = (ha - hb) * 8.0 * 1e4
    ss = pd.Series(s)
    shat = ss.rolling(FORECAST_H, min_periods=FORECAST_H).mean().to_numpy()
    m30 = ss.rolling(STATIC_H, min_periods=STATIC_H).mean().to_numpy()
    pav, pbv = pa.reindex(grid).to_numpy(float), pb.reindex(grid).to_numpy(float)
    sec = to_sec(grid)
    settle = np.union1d(to_sec(fa.index), to_sec(fb.index))
    pos = np.searchsorted(sec, settle)
    pos = pos[(pos < len(sec)) & (sec[np.clip(pos, 0, len(sec) - 1)] == settle)]
    dec = pos[np.isfinite(pav[pos]) & np.isfinite(pbv[pos])]
    return Pair(
        sym, grid, sec, pav, pbv, s, shat, m30,
        to_sec(fa.index), np.cumsum(fa.to_numpy(float)),
        to_sec(fb.index), np.cumsum(fb.to_numpy(float)),
        dec,
    )  # fmt: skip


def fund_sum(ts: np.ndarray, cum: np.ndarray, t0: int, t1: int) -> float:
    """Sum of raw settled rates with t0 < settlement <= t1 (epoch seconds)."""
    i0, i1 = np.searchsorted(ts, t0, side="right"), np.searchsorted(ts, t1, side="right")
    hi = cum[i1 - 1] if i1 > 0 else 0.0
    lo = cum[i0 - 1] if i0 > 0 else 0.0
    return float(hi - lo)


def trade_pnl(p: Pair, pos: int, k0: int, k1: int) -> dict:
    """Gross components in bps of one leg's notional for pos=+1 short A / long B."""
    if not (p.pa[k0] > 0 and p.pb[k0] > 0 and p.pa[k1] > 0 and p.pb[k1] > 0):
        raise ValueError("non-positive or missing price (R18)")
    t0, t1 = int(p.sec[k0]), int(p.sec[k1])
    fa = fund_sum(p.ts_a, p.cum_a, t0, t1)
    fb = fund_sum(p.ts_b, p.cum_b, t0, t1)
    price = pos * ((p.pb[k1] / p.pb[k0] - 1.0) - (p.pa[k1] / p.pa[k0] - 1.0)) * 1e4
    return {
        "funding": pos * (fa - fb) * 1e4,
        "price": float(price),
        "hours": (t1 - t0) / 3600.0,
    }


def simulate(p: Pair, theta: float, t_lo: pd.Timestamp, t_hi: pd.Timestamp) -> tuple[list, dict]:
    """The declared rule on one symbol. Entries only inside [t_lo, t_hi]."""
    trades: list[dict] = []
    cnt = {"open_at_end": 0, "forced_gap_exits": 0}
    pos, k_in, last_exit = 0, -1, -1
    lo_s, hi_s = int(t_lo.timestamp()), int(t_hi.timestamp())
    for k in p.dec:
        sh, sv = p.shat[k], p.s[k]
        if pos:
            held_h = (p.sec[k] - p.sec[k_in]) / 3600.0
            nan = not np.isfinite(sh)
            if nan or abs(sh) < theta / 2.0 or held_h >= MAX_HOLD_H:
                reason = "gap" if nan else ("time" if held_h >= MAX_HOLD_H else "revert")
                cnt["forced_gap_exits"] += int(nan)
                row = trade_pnl(p, pos, k_in, k)
                row.update(
                    sym=p.sym, pos=pos, k_in=k_in, k_out=k, reason=reason,
                    t_in=p.grid[k_in], t_out=p.grid[k],
                    year=p.grid[k_in].year, s_in=float(p.s[k_in]), shat_in=float(p.shat[k_in]),
                )  # fmt: skip
                trades.append(row)
                pos, last_exit = 0, k
            continue
        if k == last_exit or not (lo_s <= p.sec[k] <= hi_s):
            continue
        if (
            np.isfinite(sh) and np.isfinite(sv)
            and abs(sh) >= theta and abs(sv) >= theta and np.sign(sh) == np.sign(sv)
        ):  # fmt: skip
            pos, k_in = (1 if sh > 0 else -1), k
    if pos:
        cnt["open_at_end"] += 1
    return trades, cnt


# ---------------------------------------------------------------- statistics


def net(rows: list[dict], level: str, mult: float) -> np.ndarray:
    c = cost_bps(level, mult)
    return np.array([r["price"] + r["funding"] - c for r in rows], dtype=float)


def stats(rows: list[dict], level: str, mult: float) -> dict:
    if not rows:
        return {"n": 0, "mean": 0.0, "t": 0.0, "by_year": {}}
    x = net(rows, level, mult)
    yrs = np.array([r["year"] for r in rows])
    sd = x.std(ddof=1) if len(x) > 1 else 0.0
    return {
        "n": len(x),
        "mean": round(float(x.mean()), 3),
        "t": round(float(x.mean() / (sd / np.sqrt(len(x)))), 2) if sd else 0.0,
        "by_year": {int(y): round(float(x[yrs == y].mean()), 3) for y in np.unique(yrs)},
    }


def posted_report(rows: list[dict], level: str, mult: float) -> dict:
    """Return on posted capital and excess over a flat 4%/yr (gate C), reported only."""
    if not rows:
        return {"mean_excess_bps": 0.0, "ann_ret_pct": 0.0, "ann_excess_pct": 0.0}
    x = net(rows, level, mult) / 1e4 / POSTED_FRAC  # return on posted capital
    days = np.array([r["hours"] for r in rows]) / 24.0
    excess = x - BENCH * days / 365.0
    ann = float(x.sum() / (days.sum() / 365.0))
    return {
        "mean_excess_bps": round(float(excess.mean() * 1e4), 2),
        "ann_ret_pct": round(ann * 100, 2),
        "ann_excess_pct": round((ann - BENCH) * 100, 2),
    }


def drop_sets(pairs: dict[str, Pair], lo: pd.Timestamp, hi: pd.Timestamp) -> dict:
    """Top-1% symbol-days by max |s| over the evaluated window (pooled)."""
    vals: dict[tuple[str, pd.Timestamp], float] = {}
    for sym, p in pairs.items():
        m = (p.sec >= lo.timestamp()) & (p.sec <= hi.timestamp()) & np.isfinite(p.s)
        if not m.any():
            continue
        d = pd.Series(np.abs(p.s[m]), index=p.grid[m]).groupby(p.grid[m].normalize()).max()
        for day, v in d.items():
            vals[(sym, day)] = float(v)
    if not vals:
        return {"thr": float("nan"), "days": set()}
    thr = float(np.quantile(list(vals.values()), 0.99))
    return {"thr": thr, "days": {k for k, v in vals.items() if v >= thr}}


def robust_rows(rows: list[dict], drops: dict) -> list[dict]:
    ftx0, ftx1 = pd.Timestamp(FTX[0], tz="UTC"), pd.Timestamp(FTX[1], tz="UTC")
    keep = []
    for r in rows:
        if r["t_in"] <= ftx1 and r["t_out"] >= ftx0:
            continue
        days = pd.date_range(r["t_in"].normalize(), r["t_out"].normalize(), freq="D")
        if any((r["sym"], d) in drops["days"] for d in days):
            continue
        keep.append(r)
    return keep


def static_control(rows: list[dict], pairs: dict[str, Pair]) -> tuple[list[dict], list[dict]]:
    """Same entry/exit times, side = short the venue with the higher trailing-30d mean funding.
    Returns (control trades, the matched subset of real trades)."""
    ctrl, matched = [], []
    for r in rows:
        p = pairs[r["sym"]]
        m = p.m30[r["k_in"]]
        if not np.isfinite(m) or m == 0:
            continue
        pos = 1 if m > 0 else -1
        c = trade_pnl(p, pos, r["k_in"], r["k_out"])
        c.update(year=r["year"])
        ctrl.append(c)
        matched.append(r)
    return ctrl, matched


def random_control(
    rows: list[dict], pairs: dict[str, Pair], t_lo: pd.Timestamp, t_hi: pd.Timestamp
) -> list[list[dict]]:
    """REPS replications of: same symbol, random decision time, random side, same hold."""
    rng = np.random.default_rng(SEED)
    lo_s, hi_s = t_lo.timestamp(), t_hi.timestamp()
    cand, dsec = {}, {}
    for sym, p in pairs.items():
        dsec[sym] = p.sec[p.dec]
        ok = np.isfinite(p.shat[p.dec]) & (dsec[sym] >= lo_s) & (dsec[sym] <= hi_s)
        cand[sym] = p.dec[ok]
    reps = []
    for _ in range(REPS):
        out = []
        for r in rows:
            p, d = pairs[r["sym"]], cand[r["sym"]]
            for _try in range(200):
                k0 = int(d[rng.integers(len(d))])
                j = np.searchsorted(dsec[r["sym"]], p.sec[k0] + r["hours"] * 3600.0)
                if j >= len(p.dec):
                    continue
                k1 = int(p.dec[j])
                c = trade_pnl(p, int(rng.choice((-1, 1))), k0, k1)
                c.update(year=p.grid[k0].year)
                out.append(c)
                break
            else:
                raise RuntimeError(f"{r['sym']}: random control found no admissible time (R35)")
        reps.append(out)
    return reps


# ---------------------------------------------------------------- data: HTTP + caches


def _get(url: str, retries: int = 6) -> bytes | None:
    """GET with a browser UA (R25). None on 404 (a missing file is a result, not an error)."""
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            time.sleep(1.5 * (k + 1))
        except Exception:
            time.sleep(1.5 * (k + 1))
    raise RuntimeError(f"giving up on {url[:100]}")


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _ms(day: str) -> int:
    return int(pd.Timestamp(day, tz="UTC").timestamp() * 1000)


def fetch_bybit(sym: str) -> None:
    """Funding history and 1h klines, paginated backwards. Idempotent per symbol: a cache
    file that already reaches FETCH_END is not refetched."""
    end_ms = _ms(FETCH_END)
    ffile, kfile = CACHE / "funding" / f"{sym}.csv", CACHE / "kline1h" / f"{sym}.csv"
    if not _complete(ffile, end_ms, 2 * 86400 * 1000):
        rows, end = {}, end_ms
        while True:
            j = json.loads(
                _get(
                    f"{BYBIT}/funding/history?category=linear&symbol={sym}&endTime={end}&limit=200"
                )
                or b"{}"
            )
            if j.get("retCode") != 0:
                raise RuntimeError(f"bybit funding {sym}: {str(j)[:150]}")
            lst = j["result"]["list"]
            if not lst:
                break
            for r in lst:
                rows[int(r["fundingRateTimestamp"])] = float(r["fundingRate"])
            oldest = min(int(r["fundingRateTimestamp"]) for r in lst)
            if oldest >= end or oldest <= _ms(FETCH_START):
                break
            end = oldest - 1
            time.sleep(0.12)
        if not rows:
            raise RuntimeError(f"bybit {sym}: zero funding rows (R35)")
        _atomic(ffile, "ts_ms,rate\n" + "".join(f"{t},{v!r}\n" for t, v in sorted(rows.items())))
    if not _complete(kfile, end_ms, 2 * 86400 * 1000):
        rows, end = {}, end_ms
        while True:
            j = json.loads(
                _get(f"{BYBIT}/kline?category=linear&symbol={sym}&interval=60&end={end}&limit=1000")
                or b"{}"
            )
            if j.get("retCode") != 0:
                raise RuntimeError(f"bybit kline {sym}: {str(j)[:150]}")
            lst = j["result"]["list"]
            if not lst:
                break
            for r in lst:
                rows[int(r[0])] = float(r[4])
            oldest = min(int(r[0]) for r in lst)
            if oldest >= end or oldest <= _ms(FETCH_START):
                break
            end = oldest - 1
            time.sleep(0.12)
        if not rows:
            raise RuntimeError(f"bybit {sym}: zero kline rows (R35)")
        _atomic(
            kfile, "open_ts_ms,close\n" + "".join(f"{t},{v!r}\n" for t, v in sorted(rows.items()))
        )


def _complete(path: Path, end_ms: int, slack_ms: int) -> bool:
    if not path.exists():
        return False
    last = path.read_text().rstrip().rsplit("\n", 1)[-1].split(",")[0]
    return last.isdigit() and int(last) >= end_ms - slack_ms


def _okx_months() -> list[str]:
    return [str(m) for m in pd.period_range("2022-03", FETCH_END[:7], freq="M")]


def _zip_csv(blob: bytes | None) -> str | None:
    """First CSV member of a zip, or None if the bytes are not a zip (an HTML 200 is a
    missing file, not data)."""
    if not blob or blob[:4] != b"PK\x03\x04":
        return None
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return z.read(z.namelist()[0]).decode()


def fetch_okx(base: str, missing: list[str]) -> None:
    """Monthly funding files (kept as raw CSV text) and 1m candlestick files (folded to 1h on
    download so the 40 MB/symbol zips are not kept). Idempotent per file."""
    inst = f"{base}-USDT-SWAP"
    for m in _okx_months():
        ym = m.replace("-", "")
        fpath = CACHE / "okx" / "funding" / base / f"{m}.csv"
        if not fpath.exists():
            url = f"{OKX_CDN}/swaprates/monthly/{ym}/{inst}-fundingrates-{m}.zip?v=1001"
            txt = _zip_csv(_get(url))
            if txt is None or not txt.startswith("instrument_name,funding_rate,funding_time"):
                missing.append(f"funding {inst} {m}")
            elif not all(ln.startswith(inst + ",") for ln in txt.splitlines()[1:]):
                raise RuntimeError(f"{inst} {m}: unexpected instrument in funding file")
            else:
                _atomic(fpath, txt)
            time.sleep(0.1)
        kpath = CACHE / "okx" / "kline1h" / base / f"{m}.csv"
        if not kpath.exists():
            url = f"{OKX_CDN}/candlesticks/monthly/{ym}/{inst}-candlesticks-{m}.zip?v=1001"
            txt = _zip_csv(_get(url))
            if txt is None or not txt.startswith("instrument_name,open,high,low,close"):
                missing.append(f"candles {inst} {m}")
            else:
                df = pd.read_csv(
                    io.StringIO(txt), usecols=["instrument_name", "close", "open_time", "confirm"]
                )
                if not (df["instrument_name"] == inst).all():
                    raise RuntimeError(f"{inst} {m}: unexpected instrument in candle file")
                df = df[df["confirm"] == 1]
                t = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
                s = pd.Series(df["close"].to_numpy(float), index=t)
                g = s.resample("1h", label="right")
                last, tmax = (
                    g.last(),
                    pd.Series(s.index, index=s.index).resample("1h", label="right").max(),
                )
                ok = (tmax + pd.Timedelta(minutes=1)) == last.index  # last bar must be minute :59
                h = last[ok.to_numpy()].dropna()
                _atomic(kpath, "close_ts_s,close\n" + "".join(
                    f"{int(i.timestamp())},{v!r}\n" for i, v in h.items()))  # fmt: skip
            time.sleep(0.15)


# ---------------------------------------------------------------- data: venue loaders


def _first_month(symbol: str) -> str:
    files = sorted((BIN_KLINES / symbol / "1m").glob(f"{symbol}-1m-*.zip"))
    if not files:
        raise RuntimeError(f"{symbol}: no Binance 1m files (R35)")
    return files[0].stem[-7:]


@lru_cache(maxsize=64)
def load_binance(sym: str, lo: str, hi: str) -> tuple[pd.Series, pd.Series]:
    start = max(lo, _first_month(sym) + "-01")
    end = min(hi, BINANCE_LAST)
    return binance_funding(sym, start, end), binance_hourly(sym, start, end)


def load_bybit(sym: str, lo: str, hi: str) -> tuple[pd.Series, pd.Series]:
    f = pd.read_csv(CACHE / "funding" / f"{sym}.csv")
    k = pd.read_csv(CACHE / "kline1h" / f"{sym}.csv")
    fs = pd.Series(f["rate"].to_numpy(float), index=pd.to_datetime(f["ts_ms"], unit="ms", utc=True))
    # Bybit kline timestamps are bar OPEN times: close time = open + 1h (R8).
    px = pd.Series(
        k["close"].to_numpy(float),
        index=pd.to_datetime(k["open_ts_ms"], unit="ms", utc=True) + pd.Timedelta(hours=1),
    )
    cut = slice(pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC") + pd.Timedelta(days=1))
    fs, px = fs.sort_index().loc[cut], px.sort_index().loc[cut]
    return fs[~fs.index.duplicated()], px[~px.index.duplicated()]


def load_okx(base: str, lo: str, hi: str) -> tuple[pd.Series, pd.Series]:
    fr, pr = [], []
    for m in _okx_months():
        fp = CACHE / "okx" / "funding" / base / f"{m}.csv"
        kp = CACHE / "okx" / "kline1h" / base / f"{m}.csv"
        if fp.exists():
            fr.append(pd.read_csv(fp))
        if kp.exists():
            pr.append(pd.read_csv(kp))
    if not fr or not pr:
        raise RuntimeError(f"OKX {base}: no cached files (R35)")
    f, k = pd.concat(fr), pd.concat(pr)
    fs = pd.Series(
        f["funding_rate"].to_numpy(float),
        index=pd.to_datetime(f["funding_time"].astype("int64"), unit="ms", utc=True),
    )
    px = pd.Series(
        k["close"].to_numpy(float), index=pd.to_datetime(k["close_ts_s"], unit="s", utc=True)
    )
    cut = slice(pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC") + pd.Timedelta(days=1))
    # OKX settlement times carry seconds of jitter (and one irregular row on 2022-12-18); round to
    # the hour as the Binance loader does, so settlements land on the hourly grid.
    fs.index = fs.index.round("h")
    fs, px = fs.sort_index().loc[cut], px.sort_index().loc[cut]
    return fs[~fs.index.duplicated()], px[~px.index.duplicated()]


def ensure_data(pair: str) -> list[str]:
    missing: list[str] = []
    for s in SYMBOLS:
        if pair == "bybit":
            fetch_bybit(s)
        else:
            fetch_okx(s[:-4], missing)
    return missing


def build_pairs(pair: str, oos: bool) -> dict[str, Pair]:
    """All ten symbols for one pair. Data are loaded 45 days before the window (warm-up for the
    72 h forecast and the 30-day control) and 20 days after it (so a position entered at the
    window's end can still exit); trades are only ENTERED inside the window."""
    kind = "oos" if oos else "is"
    # one Binance load shared by both pairs (cache key): widest window of the two
    lo = str((pd.Timestamp(PAIRS["bybit"][kind][0]) - pd.Timedelta(days=45)).date())
    hi = str((pd.Timestamp(PAIRS["bybit"][kind][1]) + pd.Timedelta(days=20)).date())
    venue = PAIRS[pair]["venue"]
    out = {}
    for sym in SYMBOLS:
        fa, pa = load_binance(sym, lo, hi)
        fb, pb = load_bybit(sym, lo, hi) if venue == "bybit" else load_okx(sym[:-4], lo, hi)
        out[sym] = make_pair(sym, fa, fb, pa, pb)
    return out


# ---------------------------------------------------------------- one cell, one pair


def cell(pairs: dict[str, Pair], theta: float, window: tuple[str, str], oos: bool) -> dict:
    t_lo = pd.Timestamp(window[0], tz="UTC")
    t_hi = pd.Timestamp(window[1] + " 23:59:59", tz="UTC")
    rows, counters = [], defaultdict(int)
    for p in pairs.values():
        r, c = simulate(p, theta, t_lo, t_hi)
        rows += r
        for k, v in c.items():
            counters[k] += v
    years = (t_hi - t_lo).total_seconds() / (365.25 * 86400)
    out: dict = {
        "theta": theta, "trades": len(rows), "counters": dict(counters),
        "by_year_n": {int(y): int(sum(r["year"] == y for r in rows))
                      for y in sorted({r["year"] for r in rows})},
        "mean_hours": round(float(np.mean([r["hours"] for r in rows])), 1) if rows else 0.0,
        "mean_price_bps": round(float(np.mean([r["price"] for r in rows])), 2) if rows else 0.0,
        "mean_funding_bps": round(float(np.mean([r["funding"] for r in rows])), 2) if rows else 0.0,
        "reasons": {k: int(sum(r["reason"] == k for r in rows)) for k in ("revert", "time", "gap")},
        "net": {f"{lv}_{m:g}x": stats(rows, lv, m) for lv in COST_BPS for m in (1.0, 2.0)},
        "posted": {f"{lv}_{m:g}x": posted_report(rows, lv, m)
                   for lv, m in (("maker", 1.0), ("maker", 2.0), ("taker", 1.0))},
        "window_years": round(years, 2),
    }  # fmt: skip
    first = {
        s: str(p.grid[p.dec[np.isfinite(p.shat[p.dec])][0]].date())
        if np.isfinite(p.shat[p.dec]).any()
        else None
        for s, p in pairs.items()
    }
    out["first_decision"] = first
    lv, mult = GATE_COST
    g = out["net"][f"{lv}_{mult:g}x"]
    # (4) robustness
    drops = drop_sets(pairs, t_lo, t_hi)
    rob = robust_rows(rows, drops)
    out["robust"] = {"thr_abs_s": round(drops["thr"], 3), "dropped_days": len(drops["days"]),
                     "n": len(rob), "stats": stats(rob, lv, mult)}  # fmt: skip
    # (5) controls
    sc, matched = static_control(rows, pairs)
    rc = random_control(rows, pairs, t_lo, t_hi)
    rand_means = [float(net(r, lv, mult).mean()) if r else 0.0 for r in rc]
    out["controls"] = {
        "static_n": len(sc), "static_mean": stats(sc, lv, mult)["mean"],
        "strategy_on_static_subset": stats(matched, lv, mult)["mean"],
        "random_mean": round(float(np.mean(rand_means)), 3),
        "random_sd_over_reps": round(float(np.std(rand_means)), 3),
    }  # fmt: skip
    c = out["controls"]
    crit = {
        "1_maker2x_mean_pos_t2": bool(g["mean"] > 0 and g["t"] >= 2),
        "4_robust_mean_pos": bool(out["robust"]["stats"]["mean"] > 0 and len(rob) > 0),
        "5_beats_both_controls": bool(
            len(sc) > 0
            and c["strategy_on_static_subset"] > c["static_mean"]
            and g["mean"] > c["random_mean"]
        ),
    }
    if not oos:
        y23 = g["by_year"].get(2023)
        n23 = out["by_year_n"].get(2023, 0)
        out["n_2023"] = n23
        out["mean_2023"] = y23
        crit["2_2023_mean_pos_n30"] = bool(y23 is not None and y23 > 0 and n23 >= MIN_TRADES_2023)
        crit["3_trades_2023_ge50"] = bool(n23 >= MIN_TRADES_YEAR)
    else:
        full = [
            y for y in range(int(window[0][:4]), int(window[1][:4]) + 1)
            if pd.Timestamp(f"{y}-01-01", tz="UTC") >= t_lo and pd.Timestamp(f"{y}-12-31", tz="UTC") <= t_hi
        ]  # fmt: skip
        per_year = len(rows) / years if years else 0.0
        out["trades_per_year"] = round(per_year, 1)
        crit["3_trades_per_year_ge50"] = bool(
            per_year >= MIN_TRADES_YEAR
            and all(out["by_year_n"].get(y, 0) >= MIN_TRADES_YEAR for y in full)
        )  # fmt: skip
    out["criteria"] = crit
    out["passes"] = bool(all(crit.values()))
    return out


def fmt_cell(pair: str, c: dict) -> list[str]:
    n = c["net"]
    lines = [
        f"-- {pair} theta={c['theta']:g} bps/8h: {c['trades']} trades "
        f"(by year {c['by_year_n']}), mean hold {c['mean_hours']}h, exits {c['reasons']}, "
        f"{c['counters']}",
        f"   gross mean: price {c['mean_price_bps']} bps, funding {c['mean_funding_bps']} bps",
        "   mean bps/trade (t)      1x                 2x",
    ]
    for lv in COST_BPS:
        a, b = n[f"{lv}_1x"], n[f"{lv}_2x"]
        lines.append(
            f"   {lv:<11} {a['mean']:>9.2f} ({a['t']:>5.2f})  {b['mean']:>9.2f} ({b['t']:>5.2f})"
        )
    lines.append(f"   by-year maker-2x: {n['maker_2x']['by_year']}")
    if "n_2023" in c:
        lines.append(f"   2023 alone maker-2x: n={c['n_2023']} mean={c['mean_2023']}")
    else:
        lines.append(f"   trades/yr over OOS window: {c['trades_per_year']}")
    r = c["robust"]
    lines.append(
        f"   robust (no top-1% |s| days >= {r['thr_abs_s']} bps, {r['dropped_days']} symbol-days, "
        f"no FTX window): n={r['n']} maker-2x mean={r['stats']['mean']}"
    )
    k = c["controls"]
    lines.append(
        f"   controls maker-2x: random {k['random_mean']} (sd over {REPS} reps "
        f"{k['random_sd_over_reps']}); static n={k['static_n']} mean {k['static_mean']} vs "
        f"strategy on same subset {k['strategy_on_static_subset']}"
    )
    for key, lab in (("maker_1x", "maker 1x"), ("maker_2x", "maker 2x"), ("taker_1x", "taker 1x")):
        q = c["posted"][key]
        lines.append(
            f"   posted capital {lab}: mean excess/trade {q['mean_excess_bps']} bps, "
            f"annualised return {q['ann_ret_pct']}%, excess over 4% {q['ann_excess_pct']} pts"
        )
    lines.append(
        "   criteria: "
        + "; ".join(f"{k} {'PASS' if v else 'FAIL'}" for k, v in c["criteria"].items())
        + f"  => {'PASS' if c['passes'] else 'FAIL'}"
    )
    return lines


def verdict_is(cells: list[dict]) -> str:
    n = sum(c["passes"] for c in cells)
    return "PASS" if n == len(cells) else ("BORDERLINE" if n else "FAIL")


def run_pair(pair: str, oos: bool) -> tuple[list[str], dict | None]:
    win = PAIRS[pair]["oos" if oos else "is"]
    label = f"ar4-p0-{'OOS' if oos else 'IS'}-{pair}"
    thetas = list(THETAS)
    if oos:
        prior = sorted((ROOT / "reports" / "runs").glob(f"*-ar4-p0-IS-{pair}-*"))
        if not prior:
            raise SystemExit(f"{pair}: no IS run recorded (R37)")
        m = json.loads((prior[-1] / "metrics.json").read_text())
        thetas = [c["theta"] for c in m["cells"] if c["passes"]]
        if not thetas:
            return [f"{pair}: no IS cell passed: OOS not run"], None
    pairs = build_pairs(pair, oos)
    lines = [f"AR-4 funding differential, Binance vs {PAIRS[pair]['venue']}, "
             f"{'OOS' if oos else 'IS'} {win[0]}..{win[1]}"]  # fmt: skip
    cells = [cell(pairs, th, win, oos) for th in thetas]
    for c in cells:
        lines += fmt_cell(pair, c)
    lines.append(f"first decision time per symbol: {cells[0]['first_decision']}")
    if oos:
        prior_cells = json.loads((prior[-1] / "metrics.json").read_text())["cells"]
        n_is = sum(c["passes"] for c in prior_cells)
        n_oos = sum(c["passes"] for c in cells)
        v = "PASS" if (n_is == len(prior_cells) and n_oos == n_is) else (
            "BORDERLINE" if n_oos else "FAIL")  # fmt: skip
        lines.append(f"{pair} OOS: {n_oos}/{n_is} IS-passing cells pass -> FINAL VERDICT: {v}")
    else:
        v = verdict_is(cells)
        lines.append(f"{pair} IS: {sum(c['passes'] for c in cells)}/{len(cells)} cells pass "
                     f"-> VERDICT: {v}")  # fmt: skip
    text = "\n".join(lines)
    path = record(
        label=label,
        spec={"pair": pair, "thetas": thetas, "window": win, "oos": oos, "costs": COST_BPS,
              "seed": SEED, "reps": REPS, "forecast_h": FORECAST_H, "max_hold_h": MAX_HOLD_H},
        metrics={"cells": cells, "verdict": v},
        extra={"script": "ntx.experiments.ar4_prototype", "rung": "P0"},
        stdout=text,
    )  # fmt: skip
    return [*lines, f"-> {path}"], {"verdict": v}


def main() -> int:
    oos = "--oos" in sys.argv
    only = [a.split("=", 1)[1] for a in sys.argv if a.startswith("--pair=")]
    names = only or list(PAIRS)
    out: list[str] = []
    ran = 0
    for name in names:
        missing = ensure_data(name)
        if missing:
            out.append(f"{name}: {len(missing)} missing data files, e.g. {missing[:4]}")
        lines, res = run_pair(name, oos)
        out += lines
        ran += res is not None
    if oos and not ran:
        out.append("no IS cell passed: OOS not run")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
