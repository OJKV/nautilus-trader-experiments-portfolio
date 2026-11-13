"""Volatility squeeze-breakout in-sample sweep. IS ONLY (R37). Funding applied (R7).

    python -m ntx.experiments.volatility_breakout_study               # the 12-cell grid
    python -m ntx.experiments.volatility_breakout_study --cell 3      # one cell, by index
    python -m ntx.experiments.volatility_breakout_study --no-kill-guard  # DIAGNOSTIC ONLY
    python -m ntx.experiments.volatility_breakout_study --tf4h           # POST-HOC, see below

`--no-kill-guard` sets `max_drawdown_equity_pct=0.0`, which disables the KILL
that otherwise latches permanently at a 20% drawdown from the high-water mark
and refuses every entry for the rest of the run. It is a DIAGNOSTIC, never a
result: a run that was killed would have been killed live too. It exists because
a killed cell stops measuring its own parameters -- most of the grid's cells died
partway through the window, on different dates, so their win rates describe
different slices of history and cannot be compared with each other. With the
guard off every cell sees the same bars, which is the only way to answer the
question the strategy exists to ask. Every such run is labelled `nokill`.

`--tf4h` was added AFTER the grid had been run and is reported as a post-hoc
addition, never as part of the pre-declared sweep. It asks one question the grid
cannot: is the squeeze signal itself weak, or is it only the 1h cost wall that
kills it? It re-runs the grid's best cell on 4h bars at the same WALL-CLOCK
horizons, and -- because translating a 20-close standard deviation to 4h leaves
only 5 closes, which is too few to measure compression with -- also re-runs it
with the indicator windows kept in bars. Reporting only whichever of the two
looks better would be exactly the cherry-picking this file exists to avoid, so
both are recorded and both are reported.

**The grid below was declared before any of it was run** and is not widened
afterwards; if it ever is, the extra cells are labelled as such in the write-up
(docs/09 §5, F024). Twelve cells is small on purpose: a wide grid guarantees one
lucky cell, and this project rejects lucky cells.

**Why these three axes and nothing else.**

* `squeeze_percentile` is the entire hypothesis, and 100.0 is the CONTROL -- the
  same strategy with the compression filter disabled. If the filtered cells do
  not show a better win rate per trade than the control at a comparable trade
  rate, the economic claim is false and must be reported false (R40). Every
  other knob is held identical between the three, and `signal_ready()` is
  deliberately independent of this knob so all three judge the same bars.
* `range_period` sets the trade rate: 24 and 48 bars are one and two days on 1h.
* `sl_atr_mult` attacks the cost wall directly. Fee drag is roughly
  `0.0020 / (stop_frac * expectancy_R)` at live config, and `stop_frac` is
  `sl_atr_mult * ATR / price` -- so a wider stop shrinks the notional that
  risk-based sizing buys, and therefore the fees, without touching the signal.

**Why the fixed values are what they are.** They are not tuned; each is copied
from something already measured:

* `timeframe="1h"` with `atr_period=56`: the scout measured that dropping to a
  faster bar while keeping `atr_period=14` halves the stop width, doubles the
  notional and doubles fee drag. 56 bars on 1h is the same wall-clock horizon as
  the incumbent's 14 bars on 4h, so the stop is the same width in percent.
* `tsl_activation_price_pct=6.0 / tsl_trail_price_pct=4.0`: the incumbent's
  1.5/1.0 on 4h, scaled by the 4x increase in how often the exit stack is
  evaluated. Left unscaled, a 1h run's mean hold collapsed from 31.5 h to 9.2 h
  and Sharpe from 0.73 to 0.13 -- that measures the exit stack, not the signal.
* `risk_equity_pct=0.25` with `max_concurrent_positions=6`: aggregate risk
  (risk x positions) is identical to the live S003 config, so the 20%
  `max_drawdown_equity_pct` KILL is no more likely to latch than it is today.
  0.25% is also about the floor at $100 equity before OKX minimums start
  rejecting orders outright.
* `max_bars_in_position=48`: two days on 1h. A volatility breakout is supposed
  to have short holds; without a time stop the trade rate is capped by however
  long the trailing stop happens to hold on.
"""

from __future__ import annotations

import argparse
import itertools
import time

from ntx.backtest import funding as fnd
from ntx.backtest.runner import BacktestSpec
from ntx.experiments._common import DATA, IS_WINDOW, evaluate, record
from ntx.experiments.s003_gate import LIVE_ASSETS, LIVE_EQUITY_USD, LIVE_SLIPPAGE_BPS
from ntx.strategies.volatility_breakout import (
    VolatilityBreakoutConfig,
    VolatilityBreakoutStrategy,
)

TIMEFRAME = "1h"
IS_DAYS = 1461  # 2020-01-01 .. 2023-12-31 inclusive, for entries/day

FIXED = dict(
    bb_period=20,
    bb_std_mult=2.0,
    squeeze_lookback=80,
    expansion_tr_mult=1.0,
    atr_period=56,
    tsl_enabled=True,
    tsl_activation_price_pct=6.0,
    tsl_trail_price_pct=4.0,
    max_bars_in_position=48,
    risk_equity_pct=0.25,
    max_leverage=3.0,
    max_concurrent_positions=6,
)

# THE PRE-DECLARED GRID. 3 x 2 x 2 = 12 cells.
GRID = {
    "squeeze_percentile": (25.0, 40.0, 100.0),  # 100.0 = filter off = the control
    "range_period": (24, 48),
    "sl_atr_mult": (2.0, 3.0),
}


def cells() -> list[dict]:
    keys = list(GRID)
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*GRID.values())]


def fee_share_pct(net_pnl: float, fees_paid: float) -> float:
    """Fees as a share of gross profit (sweep.py's definition).

    Meaningless when gross is negative -- the denominator collapses and the
    ratio explodes -- so the caller must read it together with the sign of
    gross, which is why the table prints both.
    """
    gross = net_pnl + fees_paid
    return fees_paid / abs(gross) * 100.0 if gross else float("nan")


# Post-hoc, NOT part of GRID. Both are the best grid cell (squeeze 40 / range 48
# bars / sl 2.0 ATR) moved to 4h; they differ only in how the indicator windows
# are translated, and neither was chosen after seeing the other's result.
TF4H = {
    # strict wall-clock translation: every horizon divided by 4
    "wallclock": dict(
        range_period=12,
        bb_period=5,
        squeeze_lookback=20,
        atr_period=14,
        max_bars_in_position=12,
        tsl_activation_price_pct=1.5,
        tsl_trail_price_pct=1.0,
        squeeze_percentile=40.0,
        sl_atr_mult=2.0,
    ),
    # indicator windows kept in BARS, so the compression reading has as many
    # samples as it did on 1h; only the trade horizons are divided by 4
    "barwindows": dict(
        range_period=12,
        bb_period=20,
        squeeze_lookback=80,
        atr_period=14,
        max_bars_in_position=12,
        tsl_activation_price_pct=1.5,
        tsl_trail_price_pct=1.0,
        squeeze_percentile=40.0,
        sl_atr_mult=2.0,
    ),
}

HEADER = (
    f"{'#':>2} {'sqz':>5} {'rng':>4} {'sl':>4} {'trades':>7} {'ent/d':>6} {'Sharpe':>7} "
    f"{'PF+fnd':>7} {'maxDD':>6} {'mtm%':>7} {'win%':>6} {'payoff':>7} "
    f"{'gross$':>8} {'fee/gr':>7} {'fund%':>7} {'killed':>7}"
)


def run_cell(
    idx: int,
    params: dict,
    series: dict,
    *,
    kill_guard: bool = True,
    timeframe: str = TIMEFRAME,
    label: str | None = None,
) -> dict:
    # 0.0 disables the drawdown KILL (guards.loss_state ignores a non-positive
    # limit). Diagnostic only -- see the module docstring.
    guard = {} if kill_guard else {"max_drawdown_equity_pct": 0.0}
    spec = BacktestSpec(
        strategy_cls=VolatilityBreakoutStrategy,
        config_cls=VolatilityBreakoutConfig,
        config_kwargs={**FIXED, **params, **guard},
        timeframe=timeframe,
        start=IS_WINDOW[0],
        end=IS_WINDOW[1],
        starting_equity_usd=LIVE_EQUITY_USD,
        slippage_bps_per_side=LIVE_SLIPPAGE_BPS,
        assets=LIVE_ASSETS,
    )
    t0 = time.time()
    out = evaluate(spec, series)
    m = out.metrics
    gross = m.net_pnl + m.fees_paid
    path = record(
        label or (f"s007-sweep-IS-c{idx:02d}" + ("" if kill_guard else "-nokill")),
        spec,
        out,
        script=__name__,
        window=list(IS_WINDOW),
        cell=params,
        kill_guard=kill_guard,
        timeframe=timeframe,
        entries_per_day=m.trades / IS_DAYS,
        gross_pnl_usd=gross,
        fee_share_of_gross_pct=fee_share_pct(m.net_pnl, m.fees_paid),
    )
    row = dict(
        idx=idx,
        **params,
        trades=m.trades,
        entries_per_day=m.trades / IS_DAYS,
        sharpe=m.sharpe,
        pf_after_funding=out.pf_after_funding,
        max_dd_pct=m.max_drawdown_pct,
        mtm_pct=m.return_pct,
        win_rate=m.win_rate,
        payoff=m.payoff_ratio,
        gross_usd=gross,
        fee_share_pct=fee_share_pct(m.net_pnl, m.fees_paid),
        funding_pct_of_gross=out.funding_impact.pct_of_gross_pnl,
        killed=int(out.counters.get("entry_skips", {}).get("killed", 0)),
        counters=out.counters,
        red_flags=m.red_flags(),
        run=str(path.name),
        secs=time.time() - t0,
    )
    print(
        f"{idx:>2} {params['squeeze_percentile']:>5.0f} {params['range_period']:>4} "
        f"{params['sl_atr_mult']:>4.1f} {m.trades:>7} {row['entries_per_day']:>6.2f} "
        f"{m.sharpe:>7.2f} {out.pf_after_funding:>7.2f} {m.max_drawdown_pct:>6.1f} "
        f"{m.return_pct:>7.1f} {m.win_rate * 100:>6.1f} {m.payoff_ratio:>7.2f} "
        f"{gross:>8.1f} {row['fee_share_pct']:>7.0f} "
        f"{out.funding_impact.pct_of_gross_pnl:>7.1f} {row['killed']:>7}",
        flush=True,
    )
    # R40: a cell whose entries were refused by a guard or by OKX minimums is not
    # measuring its parameters, so the reasons are printed, never just the totals.
    print(
        f"   -> {path.name}  ({row['secs']:.0f}s)  rejects={out.counters.get('rejects', {})} "
        f"skips={out.counters.get('entry_skips', {})} exits={out.counters.get('exits', {})}",
        flush=True,
    )
    if row["red_flags"]:
        print(f"   !! red flags: {row['red_flags']}", flush=True)
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="volatility squeeze breakout IS sweep")
    ap.add_argument("--cell", type=int, action="append", help="run only these cell indices")
    ap.add_argument(
        "--tf4h",
        action="store_true",
        help="POST-HOC: re-run the best grid cell on 4h bars, two window translations",
    )
    ap.add_argument(
        "--no-kill-guard",
        action="store_true",
        help="DIAGNOSTIC: disable the 20%% drawdown KILL so every cell sees the same bars",
    )
    args = ap.parse_args(argv)

    grid = cells()
    chosen = args.cell if args.cell else list(range(len(grid)))
    series = fnd.load_funding_series(DATA / "raw")  # loaded once, reused across cells
    if args.tf4h:
        print("POST-HOC 4h check (NOT part of the pre-declared grid)\n")
        print(HEADER)
        print("-" * len(HEADER))
        for i, (name, params) in enumerate(TF4H.items()):
            run_cell(
                i,
                params,
                series,
                kill_guard=not args.no_kill_guard,
                timeframe="4h",
                label=f"s007-tf4h-{name}-IS" + ("" if not args.no_kill_guard else "-nokill"),
            )
        return 0
    print(
        f"squeeze breakout, {TIMEFRAME}, IS {IS_WINDOW[0]}..{IS_WINDOW[1]}, "
        f"${LIVE_EQUITY_USD:,.2f} on {len(LIVE_ASSETS)} assets, "
        f"{LIVE_SLIPPAGE_BPS:.0f}bps slippage per side, funding applied\n"
        f"fixed: {FIXED}\n"
        + (
            "\n*** DIAGNOSTIC: drawdown KILL disabled -- not a result ***\n"
            if args.no_kill_guard
            else ""
        )
    )
    print(HEADER)
    print("-" * len(HEADER))
    rows = [run_cell(i, grid[i], series, kill_guard=not args.no_kill_guard) for i in chosen]

    best = max(rows, key=lambda r: r["sharpe"])
    print(f"\nbest in-sample Sharpe: cell {best['idx']} {grid[best['idx']]} -> {best['run']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
