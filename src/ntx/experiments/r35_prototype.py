"""R3-5 P0 prototype: taker-flow imbalance -> 4-12 h continuation, 10 perps.

    python -m ntx.experiments.r35_prototype          # IS 2020-2023: decile table + grid
    python -m ntx.experiments.r35_prototype --oos    # ONCE, on the IS-passing cells only

DECLARED BEFORE THE FIRST RUN (docs/strategies/research/round3-ideas.md R3-5, with two
clarifications written down before running, 2026-10-11):
    data      Binance USD-M 1m klines on disk; taker buy volume = column 9, total = col 5.
              (Declared source was the `metrics` 5-min taker ratio: the same quantity,
              coarser. The klines give it exactly.)
    flow      per 4h window ending at 00/04/08/12/16/20 UTC: f = log(buy / sell) taker volume
    z         (f - mean) / std over the previous 540 windows (90 days), past only
    entry     |z| > Z: enter in the sign of z at the window close (R8), one position per
              symbol at a time (signals while holding are ignored)
    exit      close H later; H in {4h, 12h}; Z in {1.5, 2.0, 2.5}   -> 6 cells
    costs     taker 5 + slip 5 per side = 20 bps round trip; judged at 2x (40); funding
              charged at every 00/08/16 settlement held through (R7)
    control   the same trades with the sign reversed (De Nicola's 1-4h reversal)
    PASS      at 2x: mean > 0, t >= 2, >= 3 of 4 years positive, >= 300 trades, beats control
    OOS       2024-01..2026-08, once, only the IS-passing cells; same rules
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from ntx.backtest.manifest import record
from ntx.experiments._common import IS_WINDOW, OOS_WINDOW
from ntx.experiments.fc1_prototype import SYMBOLS
from ntx.experiments.sa1_prototype import funding

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "data" / "raw" / "klines"
ZS = (1.5, 2.0, 2.5)
HOLDS_4H = (1, 3)  # in 4h windows: 4h, 12h
Z_WINDOWS = 540
RT_COST = 20.0


def load_flow(symbol: str, start: str, end: str) -> pd.DataFrame:
    """4h bars (indexed by CLOSE time) with close, taker buy and sell volume."""
    frames = []
    for month in pd.period_range(start[:7], end[:7], freq="M"):
        path = RAW / symbol / "1m" / f"{symbol}-1m-{month}.zip"
        if not path.exists():
            continue
        with zipfile.ZipFile(path) as z:
            raw = z.read(z.namelist()[0])
        df = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 4, 5, 9], dtype=str)
        frames.append(df[df[0].str.isdigit()])
    if not frames:
        raise RuntimeError(f"{symbol}: no data (R35)")
    df = pd.concat(frames)
    t = df[0].astype("int64").values
    t = np.where(t > 10**14, t // 1000, t)
    m = pd.DataFrame(
        {"c": df[4].astype(float).values, "v": df[5].astype(float).values,
         "b": df[9].astype(float).values},
        index=pd.to_datetime(t, unit="ms", utc=True),
    )  # fmt: skip
    m = m[~m.index.duplicated()].sort_index().loc[start : f"{end} 23:59:59"]
    bars = m.resample("4h", label="right", closed="left").agg({"c": "last", "v": "sum", "b": "sum"})
    bars = bars.dropna()
    bars["s"] = bars["v"] - bars["b"]
    return bars[(bars["b"] > 0) & (bars["s"] > 0)]


def zscore(bars: pd.DataFrame) -> pd.Series:
    f = np.log(bars["b"] / bars["s"])
    mu = f.rolling(Z_WINDOWS).mean().shift(1)
    sd = f.rolling(Z_WINDOWS).std().shift(1)
    return (f - mu) / sd


def trades_for(bars, z, fund, zt: float, hold: int, sign: int = 1) -> list[dict]:
    c, zz, idx = bars["c"].values, z.values, bars.index
    out, i = [], 0
    while i < len(c) - hold:
        if np.isfinite(zz[i]) and abs(zz[i]) > zt:
            side = sign * (1 if zz[i] > 0 else -1)
            gross = side * (c[i + hold] / c[i] - 1) * 1e4
            f = fund.loc[idx[i] + pd.Timedelta(seconds=1) : idx[i + hold]].sum() * 1e4
            out.append({"t": idx[i], "year": idx[i].year, "gross": gross, "fund": -side * f})
            i += hold  # one position per symbol at a time
            continue
        i += 1
    return out


def stats(rows: list[dict], cost: float) -> dict:
    if not rows:
        return {"trades": 0, "mean_bps": 0.0, "t_stat": 0.0, "pos_years": 0, "years": 0}
    net = np.array([r["gross"] + r["fund"] for r in rows]) - cost
    yrs = np.array([r["year"] for r in rows])
    by_year = {int(y): round(float(net[yrs == y].mean()), 2) for y in np.unique(yrs)}
    sd = net.std(ddof=1)
    return {
        "trades": len(net), "mean_bps": round(float(net.mean()), 2),
        "t_stat": round(float(net.mean() / (sd / np.sqrt(len(net)))), 2) if sd else 0.0,
        "pos_years": int(sum(v > 0 for v in by_year.values())), "years": len(by_year),
        "by_year": by_year,
    }  # fmt: skip


def main() -> int:
    oos = "--oos" in sys.argv
    start, end = OOS_WINDOW if oos else IS_WINDOW
    data = {}
    for s in SYMBOLS:
        bars = load_flow(s, "2019-09-01" if not oos else "2023-09-01", end)  # z warm-up
        z = zscore(bars)
        keep = bars.index >= pd.Timestamp(start, tz="UTC")
        data[s] = (bars[keep], z[keep], funding(s, start, end))

    lines = [f"R3-5 taker-flow continuation, {'OOS' if oos else 'IS'} {start}..{end}"]
    deciles = None
    if not oos:
        # Diagnostic first: forward 4h / 12h return by imbalance decile, pooled.
        rows = []
        for bars, z, _ in data.values():
            c = bars["c"]
            rows.append(pd.DataFrame({"z": z, "f4": (c.shift(-1) / c - 1) * 1e4,
                                      "f12": (c.shift(-3) / c - 1) * 1e4}))  # fmt: skip
        d = pd.concat(rows).dropna()
        d["dec"] = pd.qcut(d["z"], 10, labels=False)
        deciles = d.groupby("dec")[["z", "f4", "f12"]].mean().round(2)
        lines.append("forward return (bps) by flow-z decile, pooled IS:")
        lines.extend("  " + ln for ln in deciles.to_string().splitlines())

    cells = [(zt, h) for zt in ZS for h in HOLDS_4H]
    if oos:
        prior = sorted((ROOT / "reports" / "runs").glob("*-r35-p0-IS-*"))
        if not prior:
            raise SystemExit("no IS run recorded (R37)")
        m = json.loads((prior[-1] / "metrics.json").read_text())
        cells = [(c["z"], c["hold_4h"]) for c in m["cells"] if c["passes"]]
        if not cells:
            print("no IS cell passed: OOS not run")
            return 0
    lines.append(f"{'Z':>4} {'H':>4} {'n':>6} {'gross':>7} {'net2x':>7} {'t2x':>6} {'yrs':>5} "
                 f"{'ctrl2x':>7}")  # fmt: skip
    out_cells = []
    for zt, h in cells:
        rows, ctrl = [], []
        for bars, z, fund in data.values():
            rows += trades_for(bars, z, fund, zt, h, +1)
            ctrl += trades_for(bars, z, fund, zt, h, -1)
        g, n2, c2 = stats(rows, 0.0), stats(rows, 2 * RT_COST), stats(ctrl, 2 * RT_COST)
        need = 3 if n2["years"] >= 4 else max(n2["years"] - 1, 1)
        ok = bool(n2["mean_bps"] > 0 and n2["t_stat"] >= 2 and n2["pos_years"] >= need
                  and n2["trades"] >= (300 if not oos else 150)
                  and n2["mean_bps"] > c2["mean_bps"])  # fmt: skip
        out_cells.append({"z": zt, "hold_4h": h, "passes": ok, "gross": g, "net_2x": n2,
                          "net_1x": stats(rows, RT_COST), "control_2x": c2})  # fmt: skip
        lines.append(f"{zt:>4} {4 * h:>3}h {n2['trades']:>6} {g['mean_bps']:>7.1f} "
                     f"{n2['mean_bps']:>7.1f} {n2['t_stat']:>6.2f} "
                     f"{n2['pos_years']:>2}/{n2['years']} {c2['mean_bps']:>7.1f}"
                     + ("  PASS" if ok else ""))  # fmt: skip
    n_pass = sum(c["passes"] for c in out_cells)
    verdict = "PASS" if n_pass >= (1 if oos else 2) else ("BORDERLINE" if n_pass else "FAIL")
    lines.append(f"cells passing: {n_pass}/{len(out_cells)} -> VERDICT: {verdict}")
    text = "\n".join(lines)
    print(text)
    path = record(
        label="r35-p0-OOS" if oos else "r35-p0-IS",
        spec={
            "z": ZS,
            "hold_4h": HOLDS_4H,
            "z_windows": Z_WINDOWS,
            "rt_cost_bps": RT_COST,
            "window": (start, end),
            "oos": oos,
        },
        metrics={
            "cells": out_cells,
            "verdict": verdict,
            "deciles": deciles.to_dict() if deciles is not None else None,
        },
        extra={"script": "ntx.experiments.r35_prototype", "rung": "P0"},
        stdout=text,
    )
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
