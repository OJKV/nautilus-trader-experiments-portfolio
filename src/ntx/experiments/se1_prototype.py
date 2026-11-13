"""SE-1 P0 prototype: long BTC/ETH perps 22:00 -> 23:59 UTC. IN-SAMPLE ONLY (R37).

    python -m ntx.experiments.se1_prototype          # IS 2020-2023, recorded (R42)

Rung P0 of `docs/strategies/research/backtest-plan-ab.md`: a standalone pandas study
on free data, nothing in the live path. The idea and its window come from the
literature (`small-capital-additions.md` SE-1), not from us, so the 22:00 hour is
not mined here; the placebo below still shows where 22:00 ranks among all 24
start hours, because a published effect can decay.

DECLARED BEFORE THE FIRST RUN -- nothing below was tuned:
    symbols   BTCUSDT, ETHUSDT (Binance USD-M 1m dumps, data/raw/klines)
    entry     open of the 22:00 UTC minute; exit close of the 23:59 minute
    funding   none by construction: OKX/Binance settle at 00/08/16 UTC, outside
              the hold (R7 is satisfied by the window, and stated here)
    costs     taker: 5 bps fee + 5 bps slippage per side (20 bps round trip)
              maker: 2 bps fee per side, no slippage (4 bps) -- assumes every
              limit order fills, which is optimistic; P1 must model it (PLAT-4)
              both also at 2x
    PASS      maker net > 0 at 2x costs, t-stat >= 2, positive in >= 3 of 4 years
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from ntx.backtest.manifest import record
from ntx.experiments._common import IS_WINDOW

RAW = Path(__file__).resolve().parents[3] / "data" / "raw" / "klines"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
ENTRY_HOUR = 22
HOLD_MIN = 120
COSTS_BPS = {"taker": 2 * (5 + 5), "maker": 2 * 2}  # round trip


def load_1m(symbol: str, start: str, end: str) -> pd.DataFrame:
    frames = []
    for month in pd.period_range(start[:7], end[:7], freq="M"):
        path = RAW / symbol / "1m" / f"{symbol}-1m-{month}.zip"
        if not path.exists():
            raise FileNotFoundError(f"missing {path} -- a gap is not 'no signal' (R35)")
        with zipfile.ZipFile(path) as z:
            raw = z.read(z.namelist()[0])
        df = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 1, 4], dtype=str)
        df = df[df[0].str.isdigit()]  # some dumps carry a header row (docs/18)
        frames.append(df)
    df = pd.concat(frames)
    t = df[0].astype("int64")
    t = np.where(t > 10**14, t // 1000, t)  # microsecond dumps, normalised to ms
    out = pd.DataFrame(
        {"open": df[1].astype(float).values, "close": df[4].astype(float).values},
        index=pd.to_datetime(t, unit="ms", utc=True),
    )
    out = out[~out.index.duplicated()].sort_index()
    out = out.loc[start : f"{end} 23:59:59"]
    if out.empty:
        raise RuntimeError(f"{symbol}: zero bars in {start}..{end} (R35)")
    return out


def window_returns(bars: pd.DataFrame, entry_hour: int) -> pd.Series:
    """Gross return (bps) of a long from entry_hour:00 open to +HOLD_MIN-1 close, per day."""
    entry = bars["open"][(bars.index.hour == entry_hour) & (bars.index.minute == 0)]
    exit_ts = entry.index + pd.Timedelta(minutes=HOLD_MIN - 1)
    exit_px = bars["close"].reindex(exit_ts)
    r = (exit_px.values / entry.values - 1.0) * 1e4
    s = pd.Series(r, index=entry.index.normalize()).dropna()
    return s


def stats(net_bps: pd.Series) -> dict:
    n = len(net_bps)
    mean, sd = float(net_bps.mean()), float(net_bps.std(ddof=1))
    t = mean / (sd / np.sqrt(n)) if sd > 0 else float("nan")
    by_year = net_bps.groupby(net_bps.index.year).mean().round(2).to_dict()
    return {
        "trades": n,
        "mean_bps": round(mean, 2),
        "t_stat": round(t, 2),
        "hit_rate": round(float((net_bps > 0).mean()), 3),
        "sharpe_ann": round(mean / sd * np.sqrt(365), 2) if sd > 0 else None,
        "by_year_mean_bps": {int(k): v for k, v in by_year.items()},
        "positive_years": int(sum(v > 0 for v in by_year.values())),
    }


def main() -> int:
    start, end = IS_WINDOW
    results, placebo = {}, {}
    trades = []
    for sym in SYMBOLS:
        bars = load_1m(sym, start, end)
        gross = window_returns(bars, ENTRY_HOUR)
        trades.append(gross.rename(sym))
        results[sym] = {"gross": stats(gross)}
        for name, c in COSTS_BPS.items():
            results[sym][name] = stats(gross - c)
            results[sym][f"{name}_2x"] = stats(gross - 2 * c)
        placebo[sym] = {h: round(float(window_returns(bars, h).mean()), 2) for h in range(24)}

    # The two-asset book: equal weight, one return per day.
    book = pd.concat(trades, axis=1).mean(axis=1)
    results["book"] = {"gross": stats(book)}
    for name, c in COSTS_BPS.items():
        results["book"][name] = stats(book - c)
        results["book"][f"{name}_2x"] = stats(book - 2 * c)

    m2 = results["book"]["maker_2x"]
    passed = m2["mean_bps"] > 0 and m2["t_stat"] >= 2 and m2["positive_years"] >= 3
    ranks = {
        sym: 1 + sum(v > placebo[sym][ENTRY_HOUR] for v in placebo[sym].values()) for sym in SYMBOLS
    }

    lines = [f"SE-1 P0 prototype, IS {start}..{end}, long {ENTRY_HOUR}:00 + {HOLD_MIN} min"]
    lines.append(f"{'':8} {'case':9} {'n':>5} {'mean':>7} {'t':>6} {'hit':>6} {'pos yrs':>8}")
    for sym in (*SYMBOLS, "book"):
        for case in ("gross", "maker", "maker_2x", "taker", "taker_2x"):
            s = results[sym][case]
            lines.append(
                f"{sym:8} {case:9} {s['trades']:>5} {s['mean_bps']:>7.2f} "
                f"{s['t_stat']:>6.2f} {s['hit_rate']:>6.3f} {s['positive_years']:>8}"
            )
    for sym in SYMBOLS:
        lines.append(f"placebo {sym}: 22:00 ranks {ranks[sym]}/24 by gross mean")
    lines.append(f"by year (book, maker 2x): {m2['by_year_mean_bps']}")
    lines.append(f"P0 VERDICT: {'PASS' if passed else 'FAIL'}")
    out = "\n".join(lines)
    print(out)

    path = record(
        label="se1-p0-IS",
        spec={
            "symbols": SYMBOLS,
            "entry_hour_utc": ENTRY_HOUR,
            "hold_min": HOLD_MIN,
            "costs_round_trip_bps": COSTS_BPS,
            "window": IS_WINDOW,
        },
        metrics={
            "results": results,
            "placebo_mean_bps_by_hour": placebo,
            "ranks": ranks,
            "verdict": "PASS" if passed else "FAIL",
        },
        extra={"script": "ntx.experiments.se1_prototype", "rung": "P0"},
        stdout=out,
    )
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
