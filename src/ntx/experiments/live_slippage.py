"""Live entry slippage vs the 4h bar close the backtest fills at (R8, R11).

    ssh <ssh-host> 'cd ~/ntx && sqlite3 -readonly -header -csv data/journal.db \
      "select trade_id, instrument_id, side, opened_at, entry_price from trades \
       where entry_price is not null"' > /tmp/live_trades.csv
    python -m ntx.experiments.live_slippage /tmp/live_trades.csv

The S003 gate charges 5 bps slippage per market order. NX-2 (2026-10-07) found
prices run on ~7.6 bps in the first minute after a breakout close, so a live
fill that lands even seconds late could cost more than the gate assumes. This
measures it on real fills: slippage_bps = signed (fill / close - 1), positive =
we paid worse than the close. The close comes from OKX's own public 4H candles.
"""

from __future__ import annotations

import json
import sys
import urllib.request

import pandas as pd

from ntx.backtest.manifest import record
from ntx.config.venues import BROWSER_UA

CANDLES = "https://my.okx.com/api/v5/market/history-candles?instId={}&bar=4H&after={}&limit=3"
GATE_SLIPPAGE_BPS = 5.0


def bar_close(inst: str, bar_close_ts: pd.Timestamp) -> float:
    """Close of the 4H candle that ENDS at bar_close_ts (OKX indexes by open time)."""
    open_ms = int((bar_close_ts - pd.Timedelta(hours=4)).timestamp() * 1000)
    req = urllib.request.Request(
        CANDLES.format(inst, open_ms + 1), headers={"User-Agent": BROWSER_UA}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    rows = [c for c in data.get("data", []) if int(c[0]) == open_ms]
    if not rows or float(rows[0][4]) <= 0:  # R18
        raise RuntimeError(f"no 4H candle for {inst} opening {open_ms}: {data.get('msg')}")
    return float(rows[0][4])


def main(argv: list[str]) -> int:
    trades = pd.read_csv(argv[0])
    rows = []
    for t in trades.itertuples():
        ts = pd.Timestamp(t.opened_at)
        close_ts = ts.floor("4h")
        inst = t.instrument_id.removesuffix(".OKX")
        c = bar_close(inst, close_ts)
        sign = 1 if t.side.upper() == "LONG" else -1
        slip = sign * (float(t.entry_price) / c - 1) * 1e4
        lag = (ts - close_ts).total_seconds()
        rows.append({"trade": t.trade_id, "inst": inst, "side": t.side, "close": c,
                     "fill": float(t.entry_price), "submit_lag_s": round(lag, 1),
                     "slippage_bps": round(slip, 2)})  # fmt: skip
    df = pd.DataFrame(rows)
    mean, med = float(df.slippage_bps.mean()), float(df.slippage_bps.median())
    out = df.to_string(index=False) + (
        f"\n\nn={len(df)}  mean {mean:.2f} bps  median {med:.2f} bps  "
        f"(gate assumes {GATE_SLIPPAGE_BPS} bps)  worse than gate: "
        f"{int((df.slippage_bps > GATE_SLIPPAGE_BPS).sum())}/{len(df)}"
    )
    print(out)
    path = record(
        label="live-slippage",
        spec={"source": argv[0], "gate_slippage_bps": GATE_SLIPPAGE_BPS},
        metrics={"n": len(df), "mean_bps": mean, "median_bps": med, "trades": rows},
        extra={"script": "ntx.experiments.live_slippage"},
        stdout=out,
    )
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
