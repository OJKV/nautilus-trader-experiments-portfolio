"""FC-1 P0 prototype: perp-over-spot premium convergence (cash-and-carry). IS ONLY (R37).

    python -m ntx.experiments.fc1_prototype
    python -m ntx.experiments.fc1_prototype --oos   # ONCE, for the declared candidates

OOS (2026-10-11, declared after the IS pass and before any OOS run): candidates
theta = 30 and 50 bps only, same rules, same costs; the 80 bps cell is not a
candidate (13.5 trades a year). Run once; the result stands whatever it is (R10).

Rung P0 of `docs/strategies/research/backtest-plan-ab.md`. Free data: Binance USD-M
monthly `premiumIndexKlines` 1h (downloaded on first run, checksum-verified, into
data/raw/premiumIndexKlines/) and the funding dumps already on disk. Nothing live.

DECLARED BEFORE THE FIRST RUN (whole grid reported):
    symbols  BTC ETH SOL BNB XRP DOGE ADA LINK AVAX TRX (from each listing)
    premium  p = the 1h premium index close (perp over spot, a fraction)
    entry    p > theta: SHORT perp + LONG spot, equal notional; theta in {30, 50, 80} bps.
             Positive premium only: the reverse needs borrowed spot (not on this account).
    exit     p <= 0 (converged) or 168 h held
    P&L      (p_entry - p_exit) in bps + funding RECEIVED on the short perp at each
             settlement held through (negative funding is paid) (R7)
    costs    round trip of 4 fills, bps of one leg: taker 2x(10 spot + 5 perp) + 4x5 slip
             = 50 ; maker 2x(8 + 2) = 20 ; both also at 2x
    PASS     maker-2x mean > 0, t >= 2, >= 3 of 4 years positive, AND >= 50 trades a
             year across the universe (fewer is not worth a fleet slot)
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from ntx.backtest.manifest import record
from ntx.data.binance_dumps import BASE, download
from ntx.experiments._common import IS_WINDOW, OOS_WINDOW
from ntx.experiments.sa1_prototype import funding

RAW = Path(__file__).resolve().parents[3] / "data" / "raw"
SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "TRXUSDT",
)  # fmt: skip
THETAS_BPS = (30, 50, 80)
MAX_HOLD_H = 168
COST = {"taker": 50.0, "maker": 20.0}


def premium(symbol: str, start: str, end: str) -> pd.Series:
    frames = []
    for month in pd.period_range(start[:7], end[:7], freq="M"):
        name = f"{symbol}-1h-{month}.zip"
        dest = RAW / "premiumIndexKlines" / symbol / name
        url = f"{BASE}/premiumIndexKlines/{symbol}/1h/{name}"
        path = download(url, dest)
        if path is None:
            continue  # before listing
        with zipfile.ZipFile(path) as z:
            df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), header=None, dtype=str)
        frames.append(df[df[0].str.isdigit()])
    if not frames:
        raise RuntimeError(f"{symbol}: no premium index data (R35)")
    df = pd.concat(frames)
    t = df[0].astype("int64").values
    t = np.where(t > 10**14, t // 1000, t)
    # Index by the bar's CLOSE time (R8): the value is known only when the hour ends.
    idx = pd.to_datetime(t, unit="ms", utc=True) + pd.Timedelta(hours=1)
    s = pd.Series(df[4].astype(float).values, index=idx)
    return s[~s.index.duplicated()].sort_index().loc[start : f"{end} 23:59:59"]


def simulate(p: pd.Series, f: pd.Series, theta: float) -> list[dict]:
    fr = f.reindex(p.index).fillna(0.0).values
    pv, idx = p.values, p.index
    trades, i0, fund = [], None, 0.0
    for i in range(len(pv)):
        if i0 is not None:
            fund += fr[i] * 1e4  # the short perp receives positive funding
            if pv[i] <= 0 or i - i0 >= MAX_HOLD_H:
                trades.append(
                    {
                        "entry": idx[i0],
                        "year": idx[i].year,
                        "hours": i - i0,
                        "basis_bps": (pv[i0] - pv[i]) * 1e4,
                        "funding_bps": fund,
                    }
                )
                i0, fund = None, 0.0
            continue
        if pv[i] > theta:
            i0 = i
    return trades


def summarise(trades: list[dict], cost: float, years: float) -> dict:
    if not trades:
        return {"trades": 0, "per_year": 0.0, "mean_bps": 0.0, "t_stat": 0.0, "pos_years": 0}
    net = np.array([t["basis_bps"] + t["funding_bps"] for t in trades]) - cost
    yrs = np.array([t["year"] for t in trades])
    by_year = {int(y): round(float(net[yrs == y].mean()), 2) for y in np.unique(yrs)}
    sd = net.std(ddof=1) if len(net) > 1 else 0.0
    return {
        "trades": len(net),
        "per_year": round(len(net) / years, 1),
        "mean_bps": round(float(net.mean()), 2),
        "t_stat": round(float(net.mean() / (sd / np.sqrt(len(net)))), 2) if sd else 0.0,
        "pos_years": int(sum(v > 0 for v in by_year.values())),
        "by_year": by_year,
    }


def main() -> int:
    oos = "--oos" in sys.argv
    start, end = OOS_WINDOW if oos else IS_WINDOW
    thetas = (30, 50) if oos else THETAS_BPS
    years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
    series = {s: (premium(s, start, end), funding(s, start, end)) for s in SYMBOLS}
    lines = [
        f"FC-1 P0, perp-over-spot convergence, {'OOS' if oos else 'IS'} {start}..{end}, {len(SYMBOLS)} symbols"
    ]
    lines.append(
        f"{'theta':>5} {'n':>5} {'/yr':>6} {'basis':>7} {'fund':>6} {'mk':>7} {'mk2x':>7} "
        f"{'t2x':>6} {'yrs':>4} {'taker':>7}"
    )
    cells = []
    for th in thetas:
        trades = []
        for p, f in series.values():
            trades += simulate(p, f, th / 1e4)
        res = {n: summarise(trades, c, years) for n, c in COST.items()}
        res.update({f"{n}_2x": summarise(trades, 2 * c, years) for n, c in COST.items()})
        basis = float(np.mean([t["basis_bps"] for t in trades])) if trades else 0.0
        fund = float(np.mean([t["funding_bps"] for t in trades])) if trades else 0.0
        m2 = res["maker_2x"]
        ok = bool(m2["mean_bps"] > 0 and m2["t_stat"] >= 2 and m2["pos_years"] >= 3
                  and m2["per_year"] >= 50)  # fmt: skip
        cells.append(
            {
                "theta_bps": th,
                "basis_mean_bps": basis,
                "funding_mean_bps": fund,
                "passes": ok,
                **res,
            }
        )
        lines.append(
            f"{th:>5} {m2['trades']:>5} {m2['per_year']:>6} {basis:>7.1f} {fund:>6.1f} "
            f"{res['maker']['mean_bps']:>7.1f} {m2['mean_bps']:>7.1f} {m2['t_stat']:>6.2f} "
            f"{m2['pos_years']:>4} {res['taker']['mean_bps']:>7.1f}" + ("  PASS" if ok else "")
        )
    passing = sum(c["passes"] for c in cells)
    verdict = "PASS" if passing >= 2 else ("BORDERLINE" if passing else "FAIL")
    lines.append(f"cells passing: {passing}/{len(cells)} -> P0 VERDICT: {verdict}")
    out = "\n".join(lines)
    print(out)
    path = record(
        label="fc1-p0-OOS" if oos else "fc1-p0-IS",
        spec={
            "symbols": SYMBOLS,
            "theta_bps": THETAS_BPS,
            "max_hold_h": MAX_HOLD_H,
            "cost_round_trip_bps": COST,
            "window": IS_WINDOW,
        },
        metrics={"cells": cells, "verdict": verdict},
        extra={"script": "ntx.experiments.fc1_prototype", "rung": "P0"},
        stdout=out,
    )
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
