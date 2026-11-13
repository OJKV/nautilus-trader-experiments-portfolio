"""MM-2 P0 prototype: deep volatility-scaled limit orders on mid-cap perps. IS ONLY (R37).

    python -m ntx.experiments.mm2_prototype

Rung P0 of `docs/strategies/research/backtest-plan-ab.md`. Standalone pandas on free
Binance USD-M 1m klines; nothing in the live path. MR-1 (the same fade entered with a
MARKET order after the move) runs alongside as the control: if resting orders do not
beat it, the ladder adds nothing (`small-capital-additions.md`).

DECLARED BEFORE THE FIRST RUN (whole grid reported):
    symbols  SOL AVAX LINK ADA DOGE XRP (from each listing)
    sigma    rolling std of 1h log returns over the previous 72 completed hours
    MM-2     at each hour's open P: buy limit P(1 - k.sigma), sell limit P(1 + k.sigma),
             live for that hour only. FILLED only if the 1m low (high) trades THROUGH
             the limit by 1 bp -- a touch is not a fill (queue position, PLAT-4)
    exit     close of the minute H after the fill (market); stop if price moves a further
             k.sigma against the entry (filled at the stop, market)
    MR-1     at each hour's close, if that hour moved more than k.sigma, enter the fade at
             market on the close, same H and stop
    costs    round trip, bps: MM-2 2 (maker in) + 10 (taker+slip out) = 12 ; MR-1 20 ;
             both also at 2x
    grid     k in {2, 3, 4} x H in {60, 240} min
    PASS     >= 2 MM-2 cells with 2x-cost mean > 0, t >= 2, >= 3 of 4 years positive,
             >= 100 trades, AND beating MR-1's net mean in the same cell
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
SYMBOLS = ("SOLUSDT", "AVAXUSDT", "LINKUSDT", "ADAUSDT", "DOGEUSDT", "XRPUSDT")
KS = (2.0, 3.0, 4.0)
HOLDS = (60, 240)
SIGMA_H = 72
THROUGH = 1e-4
COST = {"mm2": 12.0, "mr1": 20.0}


def load_ohlc(symbol: str, start: str, end: str) -> pd.DataFrame:
    frames = []
    for month in pd.period_range(start[:7], end[:7], freq="M"):
        path = RAW / symbol / "1m" / f"{symbol}-1m-{month}.zip"
        if not path.exists():
            continue  # before listing; an empty result raises below (R35)
        with zipfile.ZipFile(path) as z:
            raw = z.read(z.namelist()[0])
        df = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 1, 2, 3, 4], dtype=str)
        frames.append(df[df[0].str.isdigit()])
    if not frames:
        raise RuntimeError(f"{symbol}: no 1m data in {start}..{end} (R35)")
    df = pd.concat(frames)
    t = df[0].astype("int64").values
    t = np.where(t > 10**14, t // 1000, t)
    out = pd.DataFrame(
        {c: df[i].astype(float).values for i, c in zip((1, 2, 3, 4), "ohlc", strict=True)},
        index=pd.to_datetime(t, unit="ms", utc=True),
    )
    out = out[~out.index.duplicated()].sort_index().loc[start : f"{end} 23:59:59"]
    # A complete minute grid, so hour h is rows [60h, 60h+60).
    first = out.index[0].ceil("h")
    grid = pd.date_range(first, out.index[-1].floor("h") - pd.Timedelta(minutes=1), freq="1min")
    out = out.reindex(grid).ffill()
    return out


def trades_for(df: pd.DataFrame, k: float, hold: int) -> tuple[list, list]:
    o, h, lo, c = (df[x].values for x in "ohlc")
    n_hours = len(df) // 60
    hc = c[59 : n_hours * 60 : 60]  # hourly closes
    ho = o[0 : n_hours * 60 : 60]  # hourly opens
    r = np.diff(np.log(hc), prepend=np.nan)
    sig = pd.Series(r).rolling(SIGMA_H).std().shift(1).values  # completed hours only
    years = df.index.year.values
    mm, mr = [], []

    def run(entry_i: int, entry_px: float, side: int, dist: float) -> float | None:
        end_i = entry_i + hold
        if end_i >= len(c):
            return None
        stop = entry_px * (1 - side * dist)
        path_lo, path_hi = lo[entry_i + 1 : end_i + 1], h[entry_i + 1 : end_i + 1]
        hit = np.nonzero(path_lo <= stop)[0] if side > 0 else np.nonzero(path_hi >= stop)[0]
        exit_px = stop if hit.size else c[end_i]
        return side * (exit_px / entry_px - 1) * 1e4

    for hr in range(SIGMA_H + 2, n_hours - 5):
        s = sig[hr]
        if not np.isfinite(s) or s <= 0:
            continue
        dist = k * s
        base = hr * 60
        # MM-2: resting limits during hour hr.
        p = ho[hr]
        buy_lim, sell_lim = p * (1 - dist), p * (1 + dist)
        mins = slice(base, base + 60)
        b = np.nonzero(lo[mins] <= buy_lim * (1 - THROUGH))[0]
        sl = np.nonzero(h[mins] >= sell_lim * (1 + THROUGH))[0]
        for idx, side, px in ((b, 1, buy_lim), (sl, -1, sell_lim)):
            if idx.size:
                g = run(base + idx[0], px, side, dist)
                if g is not None:
                    mm.append((years[base], g))
        # MR-1: hour hr-1 moved more than k.sigma -> fade at its close (= this hour's open).
        move = np.log(hc[hr - 1] / hc[hr - 2])
        if abs(move) > dist:
            side = -1 if move > 0 else 1
            g = run(base - 1, c[base - 1], side, dist)
            if g is not None:
                mr.append((years[base], g))
    return mm, mr


def summarise(rows: list, cost: float) -> dict:
    if not rows:
        return {"trades": 0, "mean_bps": 0.0, "t_stat": 0.0, "pos_years": 0}
    yrs = np.array([y for y, _ in rows])
    net = np.array([g for _, g in rows]) - cost
    sd = net.std(ddof=1) if len(net) > 1 else 0.0
    by_year = {int(y): round(float(net[yrs == y].mean()), 2) for y in np.unique(yrs)}
    return {
        "trades": len(net),
        "mean_bps": round(float(net.mean()), 2),
        "t_stat": round(float(net.mean() / (sd / np.sqrt(len(net)))), 2) if sd else 0.0,
        "pos_years": int(sum(v > 0 for v in by_year.values())),
        "by_year": by_year,
    }


def main() -> int:
    start, end = IS_WINDOW
    data = {s: load_ohlc(s, start, end) for s in SYMBOLS}
    cells, lines = [], [f"MM-2 P0 vs MR-1 control, IS {start}..{end}, {', '.join(SYMBOLS)}"]
    lines.append(
        f"{'k':>3} {'H':>4} | {'mm n':>6} {'gross':>7} {'net2x':>7} {'t2x':>6} {'yrs':>4} | "
        f"{'mr n':>6} {'gross':>7} {'net2x':>7}"
    )
    for k in KS:
        for hold in HOLDS:
            mm, mr = [], []
            for df in data.values():
                a, b = trades_for(df, k, hold)
                mm += a
                mr += b
            g_mm, g_mr = summarise(mm, 0.0), summarise(mr, 0.0)
            m2, r2 = summarise(mm, 2 * COST["mm2"]), summarise(mr, 2 * COST["mr1"])
            ok = bool(
                m2["trades"] >= 100 and m2["mean_bps"] > 0 and m2["t_stat"] >= 2
                and m2["pos_years"] >= 3 and m2["mean_bps"] > r2["mean_bps"]
            )  # fmt: skip
            cells.append(
                {
                    "k": k,
                    "hold_min": hold,
                    "mm2_gross": g_mm,
                    "mm2_2x": m2,
                    "mm2_1x": summarise(mm, COST["mm2"]),
                    "mr1_gross": g_mr,
                    "mr1_2x": r2,
                    "passes": ok,
                }
            )
            lines.append(
                f"{k:>3} {hold:>4} | {m2['trades']:>6} {g_mm['mean_bps']:>7.1f} "
                f"{m2['mean_bps']:>7.1f} {m2['t_stat']:>6.2f} {m2['pos_years']:>4} | "
                f"{r2['trades']:>6} {g_mr['mean_bps']:>7.1f} {r2['mean_bps']:>7.1f}"
                + ("  PASS" if ok else "")
            )
    passing = sum(c["passes"] for c in cells)
    verdict = "PASS" if passing >= 2 else ("BORDERLINE" if passing else "FAIL")
    lines.append(f"cells passing: {passing}/{len(cells)} -> P0 VERDICT: {verdict}")
    out = "\n".join(lines)
    print(out)
    path = record(
        label="mm2-p0-IS",
        spec={
            "symbols": SYMBOLS,
            "k": KS,
            "hold_min": HOLDS,
            "sigma_hours": SIGMA_H,
            "through_bps": THROUGH * 1e4,
            "cost_round_trip_bps": COST,
            "window": IS_WINDOW,
        },
        metrics={"cells": cells, "verdict": verdict},
        extra={"script": "ntx.experiments.mm2_prototype", "rung": "P0"},
        stdout=out,
    )
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
