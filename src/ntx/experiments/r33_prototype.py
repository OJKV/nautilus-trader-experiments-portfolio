"""R3-3 P0 prototype: trend-follow the ETH/BTC ratio (the mirror of failed SA-1).

    python -m ntx.experiments.r33_prototype          # IS 2020-2023
    python -m ntx.experiments.r33_prototype --oos    # ONCE, on the IS-passing cells

R10 LOG (2026-10-11): this hypothesis was formed AFTER SA-1 (fade the ratio) lost
on IS because the ratio trended. A strong IS result is therefore expected by
construction and carries little weight. The OOS run is MANDATORY and happens
once, on exactly the cells that pass IS, whatever it shows.

DECLARED BEFORE THE FIRST RUN (from docs/strategies/research/round3-ideas.md R3-3):
    x        log(ETH/BTC) on daily closes (00:00 UTC), Binance USD-M 1m dumps
    signal   x > EMA_N(x) + band -> +1 (long ETH / short BTC); x < EMA_N(x) - band -> -1;
             inside the band: keep the previous position. band in {0, 1 x 30-day std of dx}
    N        {30, 60, 120} days                       -> 6 cells
    P&L      daily, position held from the prior close: pos * (r_ETH - r_BTC) in bps of
             one leg's notional, plus funding on both legs (long pays, short receives, R7)
    costs    each fill 10 bps (5 fee + 5 slippage); a flip = 4 fills, first entry 2;
             judged at 2x (20 bps per fill)
    PASS     at 2x costs: total net > 0, t >= 2 on weekly P&L, >= 3 of 4 years positive,
             >= 15 flips
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from ntx.backtest.manifest import record
from ntx.experiments._common import IS_WINDOW, OOS_WINDOW
from ntx.experiments.sa1_prototype import funding, hourly

NS = (30, 60, 120)
BANDS = (0.0, 1.0)
FILL_BPS = 10.0


def simulate(eth: pd.Series, btc: pd.Series, fe: pd.Series, fb: pd.Series, n: int,
             band_k: float, fill_bps: float) -> tuple[pd.Series, int]:  # fmt: skip
    """Daily net P&L (bps of one leg) and the number of flips."""
    x = np.log(eth / btc)
    ema = x.ewm(span=n, adjust=False).mean()
    band = band_k * x.diff().rolling(30).std()
    pos, out, flips, prev = 0, [], 0, 0
    r_e, r_b = eth.pct_change().values, btc.pct_change().values
    f_e = fe.resample("1D", label="right", closed="right").sum().reindex(x.index).fillna(0).values
    f_b = fb.resample("1D", label="right", closed="right").sum().reindex(x.index).fillna(0).values
    xv, ev, bv = x.values, ema.values, band.fillna(0).values
    for i in range(len(xv)):
        pnl = 0.0
        if i > 0 and prev:
            pnl = prev * (r_e[i] - r_b[i]) * 1e4 + prev * (-f_e[i] + f_b[i]) * 1e4
        if i >= n:  # the EMA needs its span before it means anything
            if xv[i] > ev[i] + bv[i]:
                pos = 1
            elif xv[i] < ev[i] - bv[i]:
                pos = -1
        if pos != prev:
            pnl -= (4 if prev else 2) * fill_bps
            flips += 1 if prev else 0
        prev = pos
        out.append(pnl)
    return pd.Series(out, index=x.index), flips


def judge(daily: pd.Series, flips: int) -> dict:
    weekly = daily.resample("W").sum()
    sd = float(weekly.std(ddof=1))
    by_year = daily.groupby(daily.index.year).sum().round(0)
    return {
        "total_bps": round(float(daily.sum()), 0),
        "weekly_t": round(float(weekly.mean() / (sd / np.sqrt(len(weekly)))), 2) if sd else 0.0,
        "sharpe_ann": round(float(daily.mean() / daily.std(ddof=1) * np.sqrt(365)), 2),
        "pos_years": int((by_year > 0).sum()),
        "years": len(by_year),
        "by_year": {int(k): float(v) for k, v in by_year.items()},
        "flips": flips,
        "max_dd_bps": round(float((daily.cumsum().cummax() - daily.cumsum()).max()), 0),
    }


def passes(j: dict) -> bool:
    need_years = 3 if j["years"] >= 4 else j["years"] - 1
    return bool(j["total_bps"] > 0 and j["weekly_t"] >= 2 and j["pos_years"] >= need_years
                and j["flips"] >= 15)  # fmt: skip


def main() -> int:
    oos = "--oos" in sys.argv
    start, end = OOS_WINDOW if oos else IS_WINDOW
    eth = hourly("ETHUSDT", start, end).resample("1D", label="right").last().dropna()
    btc = hourly("BTCUSDT", start, end).resample("1D", label="right").last().dropna()
    idx = eth.index.intersection(btc.index)
    eth, btc = eth[idx], btc[idx]
    fe, fb = funding("ETHUSDT", start, end), funding("BTCUSDT", start, end)
    cells = [(n, b) for n in NS for b in BANDS]
    if oos:
        prior = sorted(RUNS_GLOB())
        if not prior:
            raise SystemExit("no IS run recorded: run IS first (R37)")
        import json

        m = json.loads((prior[-1] / "metrics.json").read_text())
        cells = [(c["n"], c["band"]) for c in m["cells"] if c["passes"]]
        if not cells:
            print("no IS cell passed: OOS not run")
            return 0
    lines = [f"R3-3 ETH/BTC trend, {'OOS' if oos else 'IS'} {start}..{end}, {len(idx)} days"]
    lines.append(f"{'N':>4} {'band':>4} {'flips':>5} {'tot2x':>8} {'t_wk':>6} {'shp':>5} "
                 f"{'yrs+':>5} {'maxDD':>7} {'tot1x':>8}")  # fmt: skip
    out_cells = []
    for n, b in cells:
        d2, flips = simulate(eth, btc, fe, fb, n, b, 2 * FILL_BPS)
        d1, _ = simulate(eth, btc, fe, fb, n, b, FILL_BPS)
        j2, j1 = judge(d2, flips), judge(d1, flips)
        ok = passes(j2)
        out_cells.append({"n": n, "band": b, "passes": ok, "cost_2x": j2, "cost_1x": j1})
        lines.append(f"{n:>4} {b:>4} {flips:>5} {j2['total_bps']:>8.0f} {j2['weekly_t']:>6.2f} "
                     f"{j2['sharpe_ann']:>5.2f} {j2['pos_years']:>3}/{j2['years']} "
                     f"{j2['max_dd_bps']:>7.0f} {j1['total_bps']:>8.0f}"
                     + ("  PASS" if ok else ""))  # fmt: skip
    n_pass = sum(c["passes"] for c in out_cells)
    verdict = "PASS" if (n_pass >= (1 if oos else 2)) else ("BORDERLINE" if n_pass else "FAIL")
    lines.append(f"cells passing: {n_pass}/{len(out_cells)} -> P0 VERDICT: {verdict}")
    text = "\n".join(lines)
    print(text)
    path = record(
        label="r33-p0-OOS" if oos else "r33-p0-IS",
        spec={
            "n_days": NS,
            "band_k": BANDS,
            "fill_bps": FILL_BPS,
            "window": (start, end),
            "oos": oos,
        },
        metrics={"cells": out_cells, "verdict": verdict},
        extra={"script": "ntx.experiments.r33_prototype", "rung": "P0"},
        stdout=text,
    )
    print(f"-> {path}")
    return 0


def RUNS_GLOB():
    from pathlib import Path

    runs = Path(__file__).resolve().parents[3] / "reports" / "runs"
    return runs.glob("*-r33-p0-IS-*")


if __name__ == "__main__":
    sys.exit(main())
