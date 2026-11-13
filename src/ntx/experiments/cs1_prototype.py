"""CS-1 P0 prototype: weekly market-neutral funding carry across perps. IS ONLY (R37).

    python -m ntx.experiments.cs1_prototype

Rung P0 of `docs/strategies/research/backtest-plan-ab.md`. Free data on disk:
Binance USD-M 1m klines (hourly closes) and funding dumps. Nothing live.

DECLARED BEFORE THE FIRST RUN (whole grid reported):
    universe  BTC ETH SOL BNB XRP DOGE ADA LINK AVAX TRX (each from its listing)
    rebalance every Monday 00:00 UTC; rank by trailing funding SUM over L days
    book      SHORT the N highest-funding perps (they receive), LONG the N lowest;
              weight 1/(2N) each -> dollar-neutral; hold one week
    return    price return + funding (short receives +f, long pays f) per leg (R7)
    costs     only names that enter or leave pay: weight x fee per fill;
              taker 10 bps (5 fee + 5 slip), maker 2 bps; both also at 2x
    grid      N in {2, 3} x L in {3, 7} days
    PASS      >= 2 cells with maker-2x weekly mean > 0, t >= 2, >= 3 of 4 years positive
"""

from __future__ import annotations

import itertools
import sys

import numpy as np
import pandas as pd

from ntx.backtest.manifest import record
from ntx.experiments._common import IS_WINDOW
from ntx.experiments.fc1_prototype import SYMBOLS
from ntx.experiments.mm2_prototype import load_ohlc
from ntx.experiments.sa1_prototype import funding

NS = (2, 3)
LOOKBACK_D = (3, 7)
FEE = {"taker": 10.0, "maker": 2.0}


def weekly_book(closes: pd.DataFrame, fund: pd.DataFrame, n: int, lookback_d: int):
    """Per-week gross return (bps) and the per-week turnover cost multiplier."""
    mondays = closes.index[(closes.index.dayofweek == 0) & (closes.index.hour == 0)]
    gross, turnover, prev = [], [], {}
    for a, b in itertools.pairwise(mondays):
        trail = fund.loc[a - pd.Timedelta(days=lookback_d) : a - pd.Timedelta(hours=1)].sum()
        live = [s for s in closes.columns if np.isfinite(closes.at[a, s]) and s in trail.index]
        if len(live) < 2 * n:
            continue
        ranked = trail[live].sort_values()
        book = {s: 1 for s in ranked.index[:n]} | {s: -1 for s in ranked.index[-n:]}
        w = 1.0 / (2 * n)
        r = 0.0
        for s, side in book.items():
            px = side * (closes.at[b, s] / closes.at[a, s] - 1) * 1e4
            f = fund[s].loc[a + pd.Timedelta(hours=1) : b].sum() * 1e4  # settled while held
            r += w * (px - side * f)  # long pays f, short receives it
        changes = sum(1 for s in set(book) | set(prev) if book.get(s) != prev.get(s))
        gross.append((b, r))
        turnover.append(changes * w)
        prev = book
    idx = [t for t, _ in gross]
    return pd.Series([g for _, g in gross], index=idx), pd.Series(turnover, index=idx)


def summarise(net: pd.Series) -> dict:
    sd = float(net.std(ddof=1))
    by_year = net.groupby(net.index.year).mean().round(2)
    eq = net.cumsum()
    return {
        "weeks": len(net),
        "mean_bps": round(float(net.mean()), 2),
        "t_stat": round(float(net.mean() / (sd / np.sqrt(len(net)))), 2) if sd else 0.0,
        "sharpe_ann": round(float(net.mean() / sd * np.sqrt(52)), 2) if sd else 0.0,
        "max_dd_bps": round(float((eq.cummax() - eq).max()), 1),
        "pos_years": int((by_year > 0).sum()),
        "by_year": {int(k): float(v) for k, v in by_year.items()},
    }


def main() -> int:
    start, end = IS_WINDOW
    closes = pd.DataFrame(
        {s: load_ohlc(s, start, end)["c"].resample("1h").first() for s in SYMBOLS}
    )
    fund = pd.DataFrame({s: funding(s, start, end) for s in SYMBOLS}).fillna(0.0)
    lines = [f"CS-1 P0, weekly funding cross-section, IS {start}..{end}"]
    lines.append(
        f"{'N':>2} {'L':>2} {'wks':>4} {'gross':>7} {'mk2x':>7} {'t2x':>6} {'shp':>5} "
        f"{'yrs':>4} {'taker':>7} {'maxDD':>7}"
    )
    cells = []
    for n in NS:
        for lb in LOOKBACK_D:
            g, turn = weekly_book(closes, fund, n, lb)
            res = {"gross": summarise(g)}
            for name, fee in FEE.items():
                res[name] = summarise(g - turn * fee)
                res[f"{name}_2x"] = summarise(g - turn * 2 * fee)
            m2 = res["maker_2x"]
            ok = bool(m2["mean_bps"] > 0 and m2["t_stat"] >= 2 and m2["pos_years"] >= 3)
            cells.append({"n": n, "lookback_d": lb, "passes": ok, **res})
            lines.append(
                f"{n:>2} {lb:>2} {m2['weeks']:>4} {res['gross']['mean_bps']:>7.1f} "
                f"{m2['mean_bps']:>7.1f} {m2['t_stat']:>6.2f} {m2['sharpe_ann']:>5.2f} "
                f"{m2['pos_years']:>4} {res['taker']['mean_bps']:>7.1f} {m2['max_dd_bps']:>7.0f}"
                + ("  PASS" if ok else "")
            )
    passing = sum(c["passes"] for c in cells)
    verdict = "PASS" if passing >= 2 else ("BORDERLINE" if passing else "FAIL")
    lines.append(f"cells passing: {passing}/{len(cells)} -> P0 VERDICT: {verdict}")
    out = "\n".join(lines)
    print(out)
    path = record(
        label="cs1-p0-IS",
        spec={
            "symbols": SYMBOLS,
            "n": NS,
            "lookback_d": LOOKBACK_D,
            "fee_bps": FEE,
            "window": IS_WINDOW,
        },
        metrics={"cells": cells, "verdict": verdict},
        extra={"script": "ntx.experiments.cs1_prototype", "rung": "P0"},
        stdout=out,
    )
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
