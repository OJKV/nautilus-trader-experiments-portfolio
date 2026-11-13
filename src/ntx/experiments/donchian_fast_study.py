"""S007 fast-Donchian in-sample sweep. IN-SAMPLE ONLY (R37).

Answers one question: does shortening S003's bar buy 3-5x the trade rate without
destroying the edge? Every cell runs at the LIVE configuration -- the real $100
account, the ten instruments it can actually trade, 5 bps of slippage per side --
because that is the only configuration a promotion decision can be made on, and
because the incumbent's fee drag is 43% there versus 31% at portfolio scale.

Funding is applied to every cell (R7); every cell is recorded under
`reports/runs/` (R42); `out.counters` is printed for every cell, because a run
that the drawdown guard KILLed reports a trade count that is not the strategy's
(scout pitfall 1).

    python -m ntx.experiments.donchian_fast_study --timeframe 1h
    python -m ntx.experiments.donchian_fast_study --timeframe 15m
    python -m ntx.experiments.donchian_fast_study            # every declared cell

`OOS_WINDOW` is deliberately not imported. The out-of-sample window is touched
once, for one chosen candidate, by whoever owns the gate.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import pandas as pd

from ntx.backtest import funding as fnd
from ntx.backtest.runner import BacktestSpec
from ntx.experiments._common import DATA, IS_WINDOW, evaluate, record
from ntx.experiments.s003_gate import LIVE_ASSETS, LIVE_EQUITY_USD, LIVE_SLIPPAGE_BPS
from ntx.strategies.donchian_fast import FastDonchianConfig, FastDonchianStrategy, scaled_exits

# 2020-01-01 .. 2023-12-31 inclusive, for entries/day.
IS_DAYS = 1461

# Risk parameters held at S003's DEPLOYED values for every cell, so the only
# things varying are the bar size and the channel periods. Copied, not imported:
# this sweep must keep describing what it ran even if S003 is re-pinned.
RISK = dict(risk_equity_pct=0.5, max_leverage=3.0, max_concurrent_positions=3)

# ---------------------------------------------------------------------------
# THE GRID, declared before the first run and not widened afterwards.
#
# Two dimensions only: the bar, and the channel periods. The exit stack is not a
# free dimension -- it is derived from the bar by `scaled_exits`, because leaving
# it at S003's 4h values would mean measuring the exit stack instead of the
# signal (scout pitfall 2). 15m carries three cells rather than four purely for
# runtime: one 15m backtest costs ~4.5 minutes against ~1 minute at 1h.
# ---------------------------------------------------------------------------
GRID: dict[str, tuple[tuple[int, int], ...]] = {
    "1h": ((15, 7), (20, 10), (30, 15), (55, 20)),
    "15m": ((15, 7), (20, 10), (55, 20)),
}

COLUMNS = [
    "timeframe",
    "entry",
    "exit",
    "trades",
    "entries_per_day",
    "sharpe",
    "pf",
    "pf_after_funding",
    "maxdd_pct",
    "return_pct",
    "win_rate",
    "fees_usd",
    "gross_usd",
    "fee_share_pct",
    "funding_pct_of_gross",
    "mean_hold_h",
    "killed",
    "run",
]


# Guard settings for the DIAGNOSTIC pass. Not a grid dimension and not a
# candidate configuration: with the account guards off, a run keeps trading past
# the drawdown that would have ended it live. Its only purpose is to separate
# "the guard stopped this" from "the signal has no edge" -- when every cell of
# the real grid latches KILL, the reported trade counts and Sharpes describe the
# guard, and reporting them as the strategy's would be a lie (R40).
GUARDS_OFF = dict(max_drawdown_equity_pct=0.0, daily_loss_limit_equity_pct=0.0)


def _cell(timeframe: str, entry: int, exit_: int, series, no_guards: bool = False) -> dict:
    """One backtest at one grid point. Returns the row printed and written."""
    spec = BacktestSpec(
        strategy_cls=FastDonchianStrategy,
        config_cls=FastDonchianConfig,
        config_kwargs=dict(
            entry_period=entry,
            exit_period=exit_,
            atr_period=14,
            tsl_enabled=True,
            **scaled_exits(timeframe),
            **RISK,
            **(GUARDS_OFF if no_guards else {}),
        ),
        timeframe=timeframe,
        start=IS_WINDOW[0],
        end=IS_WINDOW[1],
        starting_equity_usd=LIVE_EQUITY_USD,
        slippage_bps_per_side=LIVE_SLIPPAGE_BPS,
        assets=LIVE_ASSETS,
    )
    out = evaluate(spec, series)
    m = out.metrics
    gross = m.net_pnl + m.fees_paid  # sweep.py:44-46, the project's definition
    # A negative gross collapses this denominator and the ratio explodes
    # meaninglessly, so it is only quoted on profitable cells (scout, costModel).
    fee_share = (m.fees_paid / abs(gross) * 100.0) if gross else float("nan")
    path = record(
        f"s007-fast-donchian-{timeframe}-{entry}-{exit_}{'-noguards' if no_guards else ''}-IS",
        spec,
        out,
        script=__name__,
        window=list(IS_WINDOW),
        grid_cell={"timeframe": timeframe, "entry_period": entry, "exit_period": exit_},
        guards_disabled=no_guards,
    )
    # Mean hold is the diagnostic that separates "the signal changed" from "the
    # exit stack now fires 16x more often" (scout pitfall 2).
    hold = out.positions
    mean_hold_h = float("nan")
    if not hold.empty and "ts_opened" in hold and "ts_closed" in hold:
        # The report's timestamp columns are not reliably typed: a run holding
        # open positions at the cut-off leaves `ts_closed` as an object column,
        # and subtracting that from a datetime column raises. Parse both.
        opened = pd.to_datetime(hold["ts_opened"], errors="coerce", utc=True)
        closed = pd.to_datetime(hold["ts_closed"], errors="coerce", utc=True)
        mean_hold_h = (closed - opened).dt.total_seconds().mean() / 3600.0
    return {
        "timeframe": timeframe,
        "entry": entry,
        "exit": exit_,
        "trades": m.trades,
        "entries_per_day": round(m.trades / IS_DAYS, 2),
        "sharpe": round(m.sharpe, 3),
        "pf": round(m.profit_factor, 3),
        "pf_after_funding": round(out.pf_after_funding, 3),
        "maxdd_pct": round(m.max_drawdown_pct, 2),
        "return_pct": round(m.return_pct, 1),
        "win_rate": round(m.win_rate, 4),
        "fees_usd": round(m.fees_paid, 2),
        "gross_usd": round(gross, 2),
        "fee_share_pct": round(fee_share, 1),
        "funding_pct_of_gross": round(out.funding_impact.pct_of_gross_pnl, 1),
        "mean_hold_h": round(mean_hold_h, 2),
        # R40 / scout pitfall 1: a latched KILL silently ends a backtest, and the
        # reported trade count then describes the guard, not the strategy.
        "killed": int(out.counters.get("entry_skips", {}).get("killed", 0)),
        "run": path.name,
        "_flags": m.red_flags(),
        "_counters": out.counters,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S007 fast Donchian IS sweep")
    ap.add_argument("--timeframe", choices=sorted(GRID), help="run only this bar size")
    ap.add_argument(
        "--cells",
        help="comma-separated 'entry/exit' subset of the declared grid, e.g. '20/10,55/20'. "
        "Selects from GRID; it cannot introduce a cell the grid does not declare.",
    )
    ap.add_argument(
        "--no-guards",
        action="store_true",
        help="DIAGNOSTIC: disable the daily-loss and drawdown guards (see GUARDS_OFF)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="append the result table to this CSV (created with a header if absent)",
    )
    args = ap.parse_args(argv)
    cells = {args.timeframe: GRID[args.timeframe]} if args.timeframe else GRID
    if args.cells:
        wanted = {tuple(int(x) for x in c.split("/")) for c in args.cells.split(",")}
        cells = {tf: tuple(c for c in periods if c in wanted) for tf, periods in cells.items()}
        missing = wanted - {c for periods in cells.values() for c in periods}
        if missing:
            raise SystemExit(f"not in the declared grid: {sorted(missing)}")

    series = fnd.load_funding_series(DATA / "raw")  # loaded once, reused
    rows: list[dict] = []
    for timeframe, periods in cells.items():
        for entry, exit_ in periods:
            t0 = time.time()
            row = _cell(timeframe, entry, exit_, series, no_guards=args.no_guards)
            rows.append(row)
            print(
                f"{timeframe:>4} {entry:>3}/{exit_:<3}{'*' if args.no_guards else ' '}"
                f"trades={row['trades']:<6} ent/day={row['entries_per_day']:<5} "
                f"Sharpe={row['sharpe']:<7} PF+f={row['pf_after_funding']:<6} "
                f"maxDD={row['maxdd_pct']:<6} fee/gross={row['fee_share_pct']:<8} "
                f"hold={row['mean_hold_h']}h killed={row['killed']}  ({time.time() - t0:.0f}s)",
                flush=True,
            )
            print(f"       counters={row['_counters']}", flush=True)
            if row["_flags"]:
                print(f"       RED FLAGS: {row['_flags']}", flush=True)
            print(f"       -> reports/runs/{row['run']}", flush=True)

    if args.out:
        out_path = Path(args.out)
        new = not out_path.exists()
        with out_path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(rows)
        print(f"\ntable -> {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
