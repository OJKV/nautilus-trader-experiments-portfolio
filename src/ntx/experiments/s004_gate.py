"""S004 cross-sectional momentum: selection robustness, then the ONE gate run.

This module is the SELECTOR's check on `cross_sectional_momentum_study.py`, not a
second sweep. Two things happen here and nothing else:

1. `--probes` -- IN-SAMPLE robustness. The study's winner sits on the CORNER of
   its declared grid (longest lookback, longest rebalance) and the study's own
   post-hoc step outside the grid (lb120/rb12) collapsed from Sharpe 1.50 to
   0.16. That step moved BOTH knobs at once, so it cannot say which one broke,
   and "one good cell next to a cliff" is the shape this project rejects (F024).
   These two probes split the step: lb120/rb6 varies lookback alone, lb90/rb12
   varies rebalance alone. They are POST-HOC and they are NOT a search -- the
   deployed parameter set is the study's winner whatever they say. They exist to
   decide whether to spend the out-of-sample touch at all.

2. `--oos` -- the single out-of-sample verification run permitted by R37/R10, at
   the winner's exact parameters and the live configuration the S003 gate uses.
   Running it a second time, or at any other parameter set, contaminates the
   window. There is no flag here that sweeps anything out-of-sample, on purpose.

    .venv/bin/python -m ntx.experiments.s004_gate --probes
    .venv/bin/python -m ntx.experiments.s004_gate --oos
"""

from __future__ import annotations

import argparse
import time

from ntx.backtest import funding as fnd
from ntx.backtest.runner import BacktestSpec
from ntx.experiments._common import DATA, IS_WINDOW, OOS_WINDOW, evaluate, record
from ntx.experiments.cross_sectional_momentum_study import (
    GATE,
    LIVE_ASSETS,
    LIVE_EQUITY_USD,
    LIVE_SLIPPAGE_BPS,
    PINNED_EXITS,
    TIMEFRAME,
    _fee_share_pct,
)
from ntx.strategies.cross_sectional_momentum import (
    CrossSectionalMomentumConfig,
    CrossSectionalMomentumStrategy,
)

# The candidate, exactly as the in-sample study's winning cell L3/lb90/rb6 ran it
# (reports/runs/20260925T092739-s004-csmom-IS-guarded-L3-lb90-rb6-613ac143). The
# production drawdown guard is left ON: a guards-off number is not a deployable
# number, and this run is the deployability question.
WINNER: dict[str, object] = dict(
    lookback=90,
    rebalance_bars=6,
    long_n=3,
    short_n=0,
    min_ranked=5,
    max_concurrent_positions=3,
    risk_equity_pct=0.5,
    **PINNED_EXITS,
)

# Probe -> the ONE knob it moves away from the winner.
PROBES: dict[str, dict[str, int]] = {
    "PROBE-lookback/L3/lb120/rb6": dict(lookback=120),
    "PROBE-rebalance/L3/lb90/rb12": dict(rebalance_bars=12),
}

OOS_DAYS = 974  # 2024-01-01 .. 2026-08-31 inclusive, for entries/day
IS_DAYS = 1461


def _spec(config_kwargs: dict, window: tuple[str, str]) -> BacktestSpec:
    return BacktestSpec(
        strategy_cls=CrossSectionalMomentumStrategy,
        config_cls=CrossSectionalMomentumConfig,
        config_kwargs=config_kwargs,
        timeframe=TIMEFRAME,
        start=window[0],
        end=window[1],
        starting_equity_usd=LIVE_EQUITY_USD,
        slippage_bps_per_side=LIVE_SLIPPAGE_BPS,
        assets=LIVE_ASSETS,
    )


def _report(name: str, m, out, days: int, fee_share: float) -> None:
    killed = out.counters.get("entry_skips", {}).get("killed", 0)
    print(
        f"{name:<30}{m.trades:>7}{m.trades / days:>7.2f}{m.sharpe:>8.2f}"
        f"{m.profit_factor:>6.2f}{out.pf_after_funding:>7.2f}{m.max_drawdown_pct:>8.1f}"
        f"{m.return_pct:>8.0f}{m.win_rate * 100:>7.1f}{fee_share:>9.0f}"
        f"{out.funding_impact.pct_of_gross_pnl:>7.1f}{killed:>8}"
    )


HEADER = (
    f"{'run':<30}{'trades':>7}{'ent/d':>7}{'Sharpe':>8}{'PF':>6}{'PF+f':>7}"
    f"{'maxDD%':>8}{'mtm%':>8}{'win%':>7}{'fee/gr%':>9}{'fund%':>7}{'killed':>8}"
)


def probes(series: dict, no_guard: bool = False) -> int:
    """In-sample only. Attributes the study's edge-of-grid collapse to one knob.

    `no_guard` is a DIAGNOSTIC, never a deployable configuration: the guarded
    probes below all latch the 20% drawdown KILL, so their Sharpe partly reports
    "how early did this die" rather than the rule's edge. Disabling the latch is
    the only way to tell a collapsed edge from a truncated run -- the same split
    the study itself makes, kept labelled so the two can never be confused.
    """
    guard = dict(max_drawdown_equity_pct=0.0) if no_guard else {}
    tag = "noguard" if no_guard else "guarded"
    state = "OFF (diagnostic, not deployable)" if no_guard else "ON (20%, as deployed)"
    print(f"S004 robustness probes, IN-SAMPLE {IS_WINDOW[0]}..{IS_WINDOW[1]}, guard {state}\n")
    print(HEADER)
    print("-" * len(HEADER))
    for name, delta in {**PROBES, "PROBE-winner/L3/lb90/rb6": {}}.items():
        t0 = time.time()
        spec = _spec({**WINNER, **delta, **guard}, IS_WINDOW)
        out = evaluate(spec, series)
        m = out.metrics
        fee_share = _fee_share_pct(m.net_pnl, m.fees_paid)
        path = record(
            f"s004-csmom-IS-{tag}-{name.replace('/', '-')}",
            spec,
            out,
            script=__name__,
            window=list(IS_WINDOW),
            cell=name,
            guard=tag,
            pre_declared=False,
            purpose="selector robustness probe, one knob off the study winner",
            fee_share_pct_of_gross=fee_share,
            entries_per_day=m.trades / IS_DAYS,
        )
        _report(name, m, out, IS_DAYS, fee_share)
        print(f"    recorded  {path.relative_to(path.parents[2])}  ({time.time() - t0:.0f}s)")
        print(f"    counters  {out.counters}")
    return 0


def oos(series: dict) -> int:
    """THE single out-of-sample run. One candidate, one parameter set, once."""
    print(
        f"S004 L3/lb90/rb6 -- OUT-OF-SAMPLE {OOS_WINDOW[0]}..{OOS_WINDOW[1]}, live config\n"
        f"${LIVE_EQUITY_USD:,.2f} on {len(LIVE_ASSETS)} assets, "
        f"{LIVE_SLIPPAGE_BPS:.0f} bps slippage/side, funding applied, guard ON\n"
    )
    print(HEADER)
    print("-" * len(HEADER))
    spec = _spec(dict(WINNER), OOS_WINDOW)
    out = evaluate(spec, series)
    m = out.metrics
    fee_share = _fee_share_pct(m.net_pnl, m.fees_paid)
    path = record(
        "s004-gate-live-OOS-L3-lb90-rb6",
        spec,
        out,
        script=__name__,
        window=list(OOS_WINDOW),
        cell="L3/lb90/rb6",
        guard="guarded",
        pre_declared=True,
        purpose="the single permitted OOS verification of the selected candidate (R37/R10)",
        fee_share_pct_of_gross=fee_share,
        entries_per_day=m.trades / OOS_DAYS,
    )
    _report("L3/lb90/rb6", m, out, OOS_DAYS, fee_share)
    print(f"    recorded  {path.relative_to(path.parents[2])}")
    print(f"    counters  {out.counters}")
    print(f"    red flags {m.red_flags()}")
    print(
        f"    fees ${m.fees_paid:,.2f} on net ${m.net_pnl:,.2f} (gross ${m.net_pnl + m.fees_paid:,.2f})"
    )

    checks = {
        "sharpe >= 0.40": m.sharpe >= GATE["sharpe"],
        "maxDD <= 40%": m.max_drawdown_pct <= GATE["maxdd"],
        "PF (after funding) >= 1.15": out.pf_after_funding >= GATE["pf"],
        "trades >= 200": m.trades >= GATE["trades"],
    }
    print("\nGATE (out-of-sample):")
    for k, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {k}")
    verdict = all(checks.values())
    print(f"\nVERDICT: {'PASS' if verdict else 'FAIL'}")
    print(f"trade rate: {m.trades / OOS_DAYS:.2f} entries/day (brief asked for 2-4)")
    return 0 if verdict else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S004 selector: robustness probes, then the gate")
    ap.add_argument("--probes", action="store_true", help="in-sample robustness probes")
    ap.add_argument(
        "--no-guard",
        action="store_true",
        help="DIAGNOSTIC for --probes: disable the latching 20%% drawdown KILL so every "
        "probe trades the whole window. Never a deployable result.",
    )
    ap.add_argument(
        "--oos",
        action="store_true",
        help="THE single out-of-sample run (R37). Do not re-run to try another parameter set.",
    )
    args = ap.parse_args(argv)
    if not (args.probes or args.oos):
        ap.error("choose --probes or --oos; neither runs by default, on purpose")
    series = fnd.load_funding_series(DATA / "raw")
    rc = probes(series, no_guard=args.no_guard) if args.probes else 0
    return oos(series) if args.oos else rc


if __name__ == "__main__":
    raise SystemExit(main())
