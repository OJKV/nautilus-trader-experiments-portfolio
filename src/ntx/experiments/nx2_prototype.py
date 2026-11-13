"""NX-2 P0 prototype: is entering a few minutes AFTER the 4h close cheaper? IS ONLY (R37).

    python -m ntx.experiments.nx2_prototype

Rung P0 of `docs/strategies/research/backtest-plan-ab.md`. Takes every S003 entry
from the clean 2026-10-06 live-config IS gate run (`reports/runs/*s003-gate-live-IS*`)
and asks what the same entry would have paid k minutes later, using Binance USD-M
1m closes (free, on disk). Nothing in the live path changes.

DECLARED BEFORE THE FIRST RUN:
    reference p0  the 4h bar's close (= close of the 1m bar ending at the signal time),
                  which is where the backtest fills (R8)
    delays k      1, 2, 3, 5, 10, 15 minutes: p_k = close of the minute ending at t+k
    cost_k (bps)  signed so that POSITIVE = the delay costs more:
                  BUY  (p_k / p0 - 1) * 1e4 ;  SELL  (p0 / p_k - 1) * 1e4
    PASS          some k with mean AND median cost_k <= -2 bps (a saving of >= 2 bps)
                  and its 90th percentile no worse than +p90 of the zero-delay spread
                  proxy (i.e. the bad tail does not grow) -- reported, judged by eye
                  only for the tail, the 2 bps bar is mechanical
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from ntx.backtest.manifest import record
from ntx.experiments._common import IS_WINDOW
from ntx.experiments.se1_prototype import load_1m

RUNS = Path(__file__).resolve().parents[3] / "reports" / "runs"
DELAYS = (1, 2, 3, 5, 10, 15)
SAVING_BAR_BPS = 2.0


def gate_entries() -> tuple[pd.DataFrame, str]:
    runs = sorted(RUNS.glob("*-s003-gate-live-IS-*"))
    if not runs:
        raise FileNotFoundError("no s003-gate-live IS run under reports/runs (R42)")
    run = runs[-1]
    t = pd.read_csv(run / "trades.csv")
    t["ts"] = pd.to_datetime(t["ts_opened"], utc=True)
    t["symbol"] = t["instrument_id"].str.replace("-USDT-SWAP.OKX", "USDT", regex=False)
    return t[["ts", "symbol", "entry"]], run.name


def delay_costs(bars: pd.DataFrame, entries: pd.DataFrame) -> pd.DataFrame:
    """cost_k in bps per entry; positive = waiting k minutes would have cost more."""
    close = bars["close"]
    # A 1m bar is indexed by its OPEN time, so the close at time t is the bar t-1m.
    p0 = close.reindex(entries["ts"] - pd.Timedelta(minutes=1)).values
    out = {}
    for k in DELAYS:
        pk = close.reindex(entries["ts"] + pd.Timedelta(minutes=k - 1)).values
        buy = entries["entry"].str.upper().eq("BUY").values
        out[k] = np.where(buy, (pk / p0 - 1) * 1e4, (p0 / pk - 1) * 1e4)
    return pd.DataFrame(out, index=entries.index)


def main() -> int:
    entries, run_name = gate_entries()
    start, end = IS_WINDOW
    entries = entries[(entries["ts"] >= start) & (entries["ts"] <= f"{end} 23:59:59")]
    parts = []
    for sym, grp in entries.groupby("symbol"):
        # Listing dates differ (AVAX: 2020-09); load from this symbol's first entry.
        first = (grp["ts"].min() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        bars = load_1m(sym, max(start, first), end)
        parts.append(delay_costs(bars, grp).assign(symbol=sym))
    costs = pd.concat(parts).dropna()
    if costs.empty:
        raise RuntimeError("no entries matched 1m data (R35)")

    rows, best = [], None
    for k in DELAYS:
        c = costs[k]
        row = {
            "k_min": k, "n": int(c.size), "mean": round(float(c.mean()), 2),
            "median": round(float(c.median()), 2), "p90": round(float(c.quantile(0.9)), 2),
            "t": round(float(c.mean() / (c.std(ddof=1) / np.sqrt(c.size))), 2),
        }  # fmt: skip
        rows.append(row)
        if row["mean"] <= -SAVING_BAR_BPS and row["median"] <= -SAVING_BAR_BPS:
            best = best or row
    by_sym = costs.groupby("symbol")[[1, 5, 15]].mean().round(2)

    lines = [f"NX-2 P0, {len(costs)} S003 IS entries from {run_name}"]
    lines.append("cost of waiting k minutes after the 4h close (bps; + = dearer, - = cheaper)")
    lines.append(f"{'k':>4} {'n':>6} {'mean':>7} {'median':>7} {'p90':>7} {'t':>6}")
    for r in rows:
        lines.append(
            f"{r['k_min']:>4} {r['n']:>6} {r['mean']:>7.2f} {r['median']:>7.2f} "
            f"{r['p90']:>7.2f} {r['t']:>6.2f}"
        )
    lines.append("by symbol, mean bps at k=1/5/15:")
    lines.extend("  " + ln for ln in by_sym.to_string().splitlines())
    verdict = "PASS" if best else "FAIL"
    lines.append(f"P0 VERDICT: {verdict}" + (f" (k={best['k_min']} min)" if best else ""))
    out = "\n".join(lines)
    print(out)
    path = record(
        label="nx2-p0-IS",
        spec={
            "delays_min": DELAYS,
            "saving_bar_bps": SAVING_BAR_BPS,
            "window": IS_WINDOW,
            "entries_from": run_name,
        },
        metrics={"rows": rows, "by_symbol": by_sym.to_dict(), "verdict": verdict},
        extra={"script": "ntx.experiments.nx2_prototype", "rung": "P0"},
        stdout=out,
    )
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
