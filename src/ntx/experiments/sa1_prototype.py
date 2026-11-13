"""SA-1 P0 prototype: ETH/BTC log-ratio z-score pair on perps. IN-SAMPLE ONLY (R37).

    python -m ntx.experiments.sa1_prototype

Rung P0 of `docs/strategies/research/backtest-plan-ab.md`: standalone pandas on free
data (Binance USD-M 1m klines resampled to 1h, Binance funding), nothing in the
live path. Market-neutral by construction -- the reason it is wanted for the fleet.

DECLARED BEFORE THE FIRST RUN (the whole grid is reported; no cell is picked alone):
    spread    s = log(ETH) - log(BTC)  (dollar-neutral, beta 1: the "ETH/BTC ratio")
    z         (s - rolling mean) / rolling std over L hours, L in {72, 168, 336}
    entry     z > +Z  -> short ETH / long BTC ;  z < -Z -> long ETH / short BTC ;  Z in {2.0, 2.5}
    exit      |z| < 0.5 (reverted), |z| > 4.0 (stop), or 168 h held (time stop)
    signal    on the close of hour t, filled at that close (R8), equal notional per leg
    costs     4 fills per round trip, in bps of ONE leg's notional:
              taker 4 x (5 fee + 5 slip) = 40 ;  maker 4 x 2 = 8 ; both also at 2x
    funding   applied per leg at every settlement held through (R7): the long leg pays
              rate x notional, the short leg receives it
    PASS      a cell with maker-2x mean > 0, t >= 2, >= 3 of 4 years positive, >= 50
              trades -- AND its grid neighbours not collapsing (judged from the table)
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
from ntx.experiments.se1_prototype import load_1m

RAW = Path(__file__).resolve().parents[3] / "data" / "raw"
LOOKBACKS = (72, 168, 336)
ENTRY_Z = (2.0, 2.5)
EXIT_Z, STOP_Z, MAX_HOLD_H = 0.5, 4.0, 168
COST_BPS = {"taker": 40.0, "maker": 8.0}


def hourly(symbol: str, start: str, end: str) -> pd.Series:
    return load_1m(symbol, start, end)["close"].resample("1h", label="right").last().dropna()


def funding(symbol: str, start: str, end: str) -> pd.Series:
    frames = []
    for month in pd.period_range(start[:7], end[:7], freq="M"):
        path = RAW / "fundingRate" / symbol / f"{symbol}-fundingRate-{month}.zip"
        if not path.exists():
            continue  # before the symbol listed
        with zipfile.ZipFile(path) as z:
            df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
        frames.append(df)
    df = pd.concat(frames)
    s = pd.Series(
        df["last_funding_rate"].astype(float).values,
        index=pd.DatetimeIndex(
            pd.to_datetime(df["calc_time"].astype("int64"), unit="ms", utc=True)
        ).round("h"),
    )
    return s[~s.index.duplicated()].sort_index()


def simulate(eth, btc, f_eth, f_btc, lookback: int, entry_z: float) -> pd.DataFrame:
    s = np.log(eth) - np.log(btc)
    z = (s - s.rolling(lookback).mean()) / s.rolling(lookback).std()
    fe = f_eth.reindex(eth.index).fillna(0.0).values
    fb = f_btc.reindex(eth.index).fillna(0.0).values
    e, b, zz, idx = eth.values, btc.values, z.values, eth.index
    trades, pos, i0 = [], 0, 0  # pos = +1 long ETH/short BTC, -1 the reverse
    fund = 0.0
    for i in range(len(idx)):
        if pos:
            # Funding settled at this hour on the position held into it.
            fund += pos * (-fe[i] + fb[i]) * 1e4
            held = i - i0
            reason = None
            if abs(zz[i]) < EXIT_Z:
                reason = "revert"
            elif abs(zz[i]) > STOP_Z:
                reason = "stop"
            elif held >= MAX_HOLD_H:
                reason = "time"
            if reason:
                gross = pos * ((e[i] / e[i0] - 1) - (b[i] / b[i0] - 1)) * 1e4
                trades.append(
                    {
                        "entry": idx[i0],
                        "exit": idx[i],
                        "side": pos,
                        "hours": held,
                        "gross_bps": gross,
                        "funding_bps": fund,
                        "reason": reason,
                    }
                )
                pos, fund = 0, 0.0
                continue
        if not pos and not np.isnan(zz[i]):
            if zz[i] > entry_z:
                pos, i0 = -1, i
            elif zz[i] < -entry_z:
                pos, i0 = 1, i
    return pd.DataFrame(trades)


def summarise(t: pd.DataFrame, cost: float) -> dict:
    if t.empty:
        return {"trades": 0}
    net = t["gross_bps"] + t["funding_bps"] - cost
    by_year = net.groupby(t["exit"].dt.year).mean().round(1)
    sd = float(net.std(ddof=1))
    return {
        "trades": len(net),
        "mean_bps": round(float(net.mean()), 2),
        "t_stat": round(float(net.mean() / (sd / np.sqrt(len(net)))), 2) if sd else None,
        "hit": round(float((net > 0).mean()), 3),
        "pos_years": int((by_year > 0).sum()),
        "by_year": {int(k): float(v) for k, v in by_year.items()},
    }


def main() -> int:
    start, end = IS_WINDOW
    eth, btc = hourly("ETHUSDT", start, end), hourly("BTCUSDT", start, end)
    idx = eth.index.intersection(btc.index)
    eth, btc = eth[idx], btc[idx]
    f_eth, f_btc = funding("ETHUSDT", start, end), funding("BTCUSDT", start, end)

    cells, lines = [], []
    lines.append(f"SA-1 P0, ETH/BTC z-pair, IS {start}..{end}, {len(idx)} hours")
    lines.append(
        f"{'L':>4} {'Z':>4} {'n':>5} {'gross':>7} {'fund':>6} {'maker':>7} {'mk2x':>7} "
        f"{'t2x':>6} {'yrs+':>5} {'taker':>7}"
    )
    for lb in LOOKBACKS:
        for ez in ENTRY_Z:
            t = simulate(eth, btc, f_eth, f_btc, lb, ez)
            res = {name: summarise(t, c) for name, c in COST_BPS.items()}
            res.update({f"{n}_2x": summarise(t, 2 * c) for n, c in COST_BPS.items()})
            gross = round(float(t["gross_bps"].mean()), 2) if len(t) else float("nan")
            fund = round(float(t["funding_bps"].mean()), 2) if len(t) else float("nan")
            m2 = res["maker_2x"]
            ok = bool(
                m2.get("trades", 0) >= 50
                and m2["mean_bps"] > 0
                and (m2["t_stat"] or 0) >= 2
                and m2["pos_years"] >= 3
            )
            cells.append(
                {
                    "lookback_h": lb,
                    "entry_z": ez,
                    "gross_mean_bps": gross,
                    "funding_mean_bps": fund,
                    "passes": ok,
                    **res,
                    "exit_reasons": t["reason"].value_counts().to_dict() if len(t) else {},
                }
            )
            lines.append(
                f"{lb:>4} {ez:>4} {res['maker'].get('trades', 0):>5} {gross:>7.1f} {fund:>6.1f} "
                f"{res['maker'].get('mean_bps', 0):>7.1f} {m2.get('mean_bps', 0):>7.1f} "
                f"{(m2.get('t_stat') or 0):>6.2f} {m2.get('pos_years', 0):>5} "
                f"{res['taker'].get('mean_bps', 0):>7.1f}" + ("  PASS" if ok else "")
            )
    passing = [c for c in cells if c["passes"]]
    verdict = "PASS" if len(passing) >= 2 else ("BORDERLINE" if passing else "FAIL")
    lines.append(f"cells passing: {len(passing)}/{len(cells)} -> P0 VERDICT: {verdict}")
    lines.append("(PASS needs >= 2 cells: one isolated cell is a peak, not a plateau)")
    out = "\n".join(lines)
    print(out)
    path = record(
        label="sa1-p0-IS",
        spec={
            "lookbacks_h": LOOKBACKS,
            "entry_z": ENTRY_Z,
            "exit_z": EXIT_Z,
            "stop_z": STOP_Z,
            "max_hold_h": MAX_HOLD_H,
            "cost_round_trip_bps": COST_BPS,
            "window": IS_WINDOW,
        },
        metrics={"cells": cells, "verdict": verdict},
        extra={"script": "ntx.experiments.sa1_prototype", "rung": "P0"},
        stdout=out,
    )
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
