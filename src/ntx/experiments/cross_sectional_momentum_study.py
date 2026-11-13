"""S004 cross-sectional momentum -- IN-SAMPLE study.

IN-SAMPLE ONLY (R37). `OOS_WINDOW` is deliberately not imported: the
out-of-sample window is touched once, for the single chosen candidate, and
choosing a candidate by looking at it makes the candidate worthless (F017).

Funding is applied to every run (R7) and every run is recorded under
`reports/runs/` (R42/R9).

    .venv/bin/python -m ntx.experiments.cross_sectional_momentum_study
    .venv/bin/python -m ntx.experiments.cross_sectional_momentum_study --only L3/lb60/rb6

THE GRID IS DECLARED BELOW, BEFORE ANY RUN. It is 12 cells plus one control.
It was not widened after seeing a result; if it ever is, the widening belongs in
the report, not in a quiet edit of this file (R10/F024).

Why these values, stated before the numbers exist:

* `lookback` 30 / 60 / 90 bars of 4h = 5 / 10 / 15 days. The documented horizon
  for cross-sectional momentum is weeks; below a couple of days the literature
  finds reversal, not momentum. 90 is also the widest lookback that keeps
  `warmup_bars()` near 100, which is what a live restart has to re-request in a
  single history call.
* `rebalance_bars` 3 / 6 bars of 4h = twice a day / once a day at 00:00 UTC.
  This is the frequency dial the whole exercise is about.
* Two side configurations, with AGGREGATE risk held constant at
  `risk_equity_pct x max_concurrent_positions = 1.5%` -- the incumbent's figure.
  Without that, a long/short cell is simply a bigger bet than a long-only one
  and the comparison measures leverage, not the rule (the scout measured exactly
  this: raising the cap at unchanged risk tripped the 20% drawdown KILL).
* Exits are pinned to S003's validated stack. Shortening the decision interval
  while also loosening the stop would mean testing the exit stack rather than
  the signal.
* Each cell is run TWICE: once as it would be deployed, and once with
  `--no-guard` (`max_drawdown_equity_pct=0`). This is not a parameter search --
  it adds no strategy knob. The default 20% drawdown guard LATCHES: once it
  fires, `_killed` stays true and every later entry is refused for the rest of
  the backtest, so a guarded run reports "how long until 20% drawdown", not the
  rule's trade rate or its edge. The guarded run is the honest deployable
  number; the unguarded run is the only way to read the rule itself, and is
  labelled `noguard` in `reports/runs/` so the two can never be confused.
* One reversal CONTROL cell at the centre of the grid. It is not a second
  strategy and not a candidate: it answers "is there any cross-sectional
  structure here at all, and does momentum have the right sign?". A momentum
  cell that barely works while its mirror image works too is measuring the exit
  stack, not the ranking.
"""

from __future__ import annotations

import argparse
import time

from ntx.backtest import funding as fnd
from ntx.backtest.runner import BacktestSpec
from ntx.experiments._common import DATA, IS_WINDOW, evaluate, record
from ntx.experiments.s003_gate import LIVE_ASSETS, LIVE_EQUITY_USD, LIVE_SLIPPAGE_BPS
from ntx.strategies.cross_sectional_momentum import (
    CrossSectionalMomentumConfig,
    CrossSectionalMomentumStrategy,
)

TIMEFRAME = "4h"
IS_DAYS = 1461  # 2020-01-01 .. 2023-12-31 inclusive, for entries/day

# The gate S003 passed. Checked here against IN-SAMPLE numbers purely as a
# screen -- the gate itself is an out-of-sample statement and is not being
# claimed by this script.
GATE = {"sharpe": 0.40, "maxdd": 40.0, "pf": 1.15, "trades": 200}

# S003's validated exit stack, pinned so a class default cannot drift in (R47).
PINNED_EXITS = dict(
    sl_atr_mult=2.0,
    atr_period=14,
    tsl_enabled=True,
    tsl_activation_price_pct=1.5,
    tsl_trail_price_pct=1.0,
    max_leverage=3.0,
)

# name -> (long_n, short_n, max_concurrent_positions, risk_equity_pct)
SIDES: dict[str, tuple[int, int, int, float]] = {
    "L3": (3, 0, 3, 0.5),
    "L3S3": (3, 3, 6, 0.25),
}
LOOKBACKS = (30, 60, 90)
REBALANCES = (3, 6)


def _cells() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for side, (long_n, short_n, max_pos, risk) in SIDES.items():
        for lookback in LOOKBACKS:
            for rebalance in REBALANCES:
                out.append(
                    (
                        f"{side}/lb{lookback}/rb{rebalance}",
                        dict(
                            lookback=lookback,
                            rebalance_bars=rebalance,
                            long_n=long_n,
                            short_n=short_n,
                            min_ranked=max(5, long_n + short_n),
                            max_concurrent_positions=max_pos,
                            risk_equity_pct=risk,
                            **PINNED_EXITS,
                        ),
                    )
                )
    # The control: grid centre, ranking inverted.
    out.append(
        (
            "CONTROL-reversal/L3/lb60/rb6",
            dict(
                lookback=60,
                rebalance_bars=6,
                long_n=3,
                short_n=0,
                min_ranked=5,
                reversal=True,
                max_concurrent_positions=3,
                risk_equity_pct=0.5,
                **PINNED_EXITS,
            ),
        )
    )
    return out


# POST-HOC, added AFTER the grid above had been run, and labelled as such in
# `reports/runs/` so no reader can mistake them for pre-declared cells (R10).
# Neither is a search for a better parameter set; both interrogate the winner:
#
#  * edge-of-grid: the declared grid's best cell sits on its CORNER (longest
#    lookback, longest rebalance), and Sharpe rose monotonically towards it. A
#    corner optimum usually means the real optimum is outside the grid. This
#    run steps one notch further out. If Sharpe keeps climbing, the "winner" is
#    just the slowest cell tested and the ranking rule is being flattered by
#    turnover, not by skill.
#  * cost stress: docs/09 §7 requires Sharpe >= 0.25 at 2x fees before go-live.
#    It is the direct test of "does this only work because costs were
#    understated", which is the question that disqualifies a candidate outright.
POSTHOC: list[tuple[str, dict, float]] = [
    (
        "POSTHOC-edge-of-grid/L3/lb120/rb12",
        dict(
            lookback=120,
            rebalance_bars=12,
            long_n=3,
            short_n=0,
            min_ranked=5,
            max_concurrent_positions=3,
            risk_equity_pct=0.5,
            **PINNED_EXITS,
        ),
        1.0,
    ),
    (
        "POSTHOC-coststress2x/L3/lb90/rb6",
        dict(
            lookback=90,
            rebalance_bars=6,
            long_n=3,
            short_n=0,
            min_ranked=5,
            max_concurrent_positions=3,
            risk_equity_pct=0.5,
            **PINNED_EXITS,
        ),
        2.0,
    ),
]


def _spec(config_kwargs: dict, fee_multiplier: float = 1.0) -> BacktestSpec:
    return BacktestSpec(
        strategy_cls=CrossSectionalMomentumStrategy,
        config_cls=CrossSectionalMomentumConfig,
        config_kwargs=config_kwargs,
        timeframe=TIMEFRAME,
        start=IS_WINDOW[0],
        end=IS_WINDOW[1],
        starting_equity_usd=LIVE_EQUITY_USD,
        slippage_bps_per_side=LIVE_SLIPPAGE_BPS,
        assets=LIVE_ASSETS,
        fee_multiplier=fee_multiplier,
    )


def _fee_share_pct(net_pnl: float, fees_paid: float) -> float:
    """Fees as a share of GROSS profit (`sweep.py:44-46`).

    Meaningless when gross is negative -- the denominator collapses and the
    ratio explodes -- so the caller must report it only for a profitable run.
    """
    gross = net_pnl + fees_paid
    return (fees_paid / abs(gross) * 100.0) if gross else float("nan")


HEADER = (
    f"{'cell':<30}{'trades':>7}{'ent/d':>7}{'Sharpe':>8}{'PF':>6}{'PF+f':>7}"
    f"{'maxDD%':>8}{'mtm%':>8}{'win%':>7}{'unrl%':>7}{'fee/gr%':>9}{'fund%':>7}{'killed':>8}"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S004 cross-sectional momentum, in-sample sweep")
    ap.add_argument("--only", action="append", help="run only these cell names (repeatable)")
    ap.add_argument(
        "--no-guard",
        action="store_true",
        help="DIAGNOSTIC: disable the latching 20%% drawdown KILL so the whole "
        "window is traded. Never a deployable result -- the guard is real.",
    )
    ap.add_argument(
        "--posthoc",
        action="store_true",
        help="run the POST-HOC robustness checks on the winner instead of the declared grid",
    )
    args = ap.parse_args(argv)

    cells = [(n, k, 1.0) for n, k in _cells()]
    if args.posthoc:
        cells = list(POSTHOC)
    if args.only:
        wanted = set(args.only)
        cells = [c for c in cells if c[0] in wanted]
        if not cells:
            raise SystemExit(f"no cell matches {sorted(wanted)}; known: {[c[0] for c in _cells()]}")

    # The guard latches (base.py `_kill_all`), so a guarded run stops trading at
    # the first 20% drawdown. Turning it off measures the rule; leaving it on
    # measures what would be deployed. Both are reported, never mixed.
    guard = dict(max_drawdown_equity_pct=0.0) if args.no_guard else {}
    tag = "noguard" if args.no_guard else "guarded"

    series = fnd.load_funding_series(DATA / "raw")  # loaded once, reused per cell
    print(
        f"S004 cross-sectional momentum, {TIMEFRAME} bars, IN-SAMPLE "
        f"{IS_WINDOW[0]}..{IS_WINDOW[1]}\n"
        f"${LIVE_EQUITY_USD:,.2f} on {len(LIVE_ASSETS)} assets, "
        f"{LIVE_SLIPPAGE_BPS:.0f} bps slippage/side, funding applied, "
        f"exits pinned to S003's stack\n"
        f"drawdown guard: {'OFF (diagnostic, not deployable)' if args.no_guard else 'ON (20%, as deployed)'}\n"
    )
    print(HEADER)
    print("-" * len(HEADER))

    rows = []
    for name, kwargs, fee_mult in cells:
        t0 = time.time()
        spec = _spec({**kwargs, **guard}, fee_multiplier=fee_mult)
        out = evaluate(spec, series)
        m = out.metrics
        killed = out.counters.get("entry_skips", {}).get("killed", 0)
        fee_share = _fee_share_pct(m.net_pnl, m.fees_paid)
        path = record(
            f"s004-csmom-IS-{tag}-{name.replace('/', '-')}",
            spec,
            out,
            script=__name__,
            window=list(IS_WINDOW),
            cell=name,
            guard=tag,
            fee_multiplier=fee_mult,
            pre_declared=not name.startswith("POSTHOC"),
            fee_share_pct_of_gross=fee_share,
            entries_per_day=m.trades / IS_DAYS,
        )
        rows.append((name, m, out, fee_share, killed, path))
        print(
            f"{name:<30}{m.trades:>7}{m.trades / IS_DAYS:>7.2f}{m.sharpe:>8.2f}"
            f"{m.profit_factor:>6.2f}{out.pf_after_funding:>7.2f}{m.max_drawdown_pct:>8.1f}"
            f"{m.return_pct:>8.0f}{m.win_rate * 100:>7.1f}"
            f"{m.unrealized_at_end_pct:>7.0f}{fee_share:>9.0f}"
            f"{out.funding_impact.pct_of_gross_pnl:>7.1f}{killed:>8}"
            f"   ({time.time() - t0:.0f}s)"
        )

    print("\nper-cell detail (R40: a knob that did nothing looks like a null result)")
    for name, m, out, _fee, _killed, path in rows:
        print(f"\n  {name}")
        print(f"    recorded  {path.relative_to(path.parents[2])}")
        print(f"    counters  {out.counters}")
        print(f"    red flags {m.red_flags()}")
        print(
            f"    fees ${m.fees_paid:,.2f} on net ${m.net_pnl:,.2f} (gross ${m.net_pnl + m.fees_paid:,.2f})"
        )

    print("\nIN-SAMPLE screen against the S003 gate thresholds")
    print("  (a screen, not the gate: the gate is an out-of-sample statement)")
    for name, m, out, _fee, _killed, _path in rows:
        checks = {
            "sharpe": m.sharpe >= GATE["sharpe"],
            "maxdd": m.max_drawdown_pct <= GATE["maxdd"],
            "pf": out.pf_after_funding >= GATE["pf"],
            "trades": m.trades >= GATE["trades"],
        }
        failed = [k for k, ok in checks.items() if not ok]
        print(f"  {'PASS' if not failed else 'fail(' + ','.join(failed) + ')':<22} {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
