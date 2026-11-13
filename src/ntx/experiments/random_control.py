"""Random-entry control: is the SIGNAL adding anything over the exit stack?

Replaces the strategy's entries with coin flips while keeping its exits, sizing
and risk layer identical, then reports where the real strategy sits in the
distribution of random outcomes.

This is the test that nearly sank S003: a 3-seed sample produced a random run at
Sharpe 1.50 out-of-sample against S003's 1.62. Forty seeds showed that was an
upper-tail draw -- and the distribution still reaches **Sharpe 1.52**, which means
**a lone backtest Sharpe between 1 and 2 is not evidence of edge on its own**.

(This docstring previously said the maximum was 2.01. That figure appears in no
recorded run; the measured maximum is 1.519, from
reports/runs/20260917T160318-random-entry-control-22e5eae5. The argument was
right, the number supporting it was not.)

Reproduces docs/strategies/S003-donchian-breakout.md §5.

    python -m ntx.experiments.random_control [--seeds 40]
"""

from __future__ import annotations

import argparse
import statistics as st

from ntx.backtest import funding as fnd
from ntx.backtest import manifest
from ntx.backtest.runner import BacktestSpec
from ntx.experiments._common import DATA, IS_WINDOW, OOS_WINDOW, evaluate
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.random_entry import RandomEntryConfig, RandomEntryStrategy
from ntx.strategies.registry import DEPLOYED

# ~1.3% of bars matches S003's realised trade frequency on this universe.
MATCHED_ENTRY_PROB = 0.013


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=40)
    args = ap.parse_args()

    series = fnd.load_funding_series(DATA / "raw")
    summary = {}
    for name, (start, end) in (("IS", IS_WINDOW), ("OOS", OOS_WINDOW)):
        base = dict(
            timeframe="4h",
            start=start,
            end=end,
            starting_equity_usd=10_000.0,
            slippage_bps_per_side=2.0,
        )
        real = evaluate(
            BacktestSpec(
                strategy_cls=DonchianBreakoutStrategy,
                config_cls=DonchianBreakoutConfig,
                config_kwargs=DEPLOYED["S003"],
                **base,
            ),
            series,
        )

        sharpes = []
        for seed in range(args.seeds):
            r = evaluate(
                BacktestSpec(
                    strategy_cls=RandomEntryStrategy,
                    config_cls=RandomEntryConfig,
                    # identical exit stack; only entries are random
                    config_kwargs=dict(
                        entry_probability=MATCHED_ENTRY_PROB,
                        seed=seed,
                        **{k: v for k, v in DEPLOYED["S003"].items() if not k.endswith("_period")},
                    ),
                    **base,
                ),
                series,
            )
            sharpes.append(r.metrics.sharpe)
        sharpes.sort()
        beat = sum(1 for s in sharpes if s < real.metrics.sharpe)
        sd = st.pstdev(sharpes)
        block = {
            "window": [start, end],
            "strategy_sharpe": real.metrics.sharpe,
            "random_n": len(sharpes),
            "random_mean": st.mean(sharpes),
            "random_sd": sd,
            "random_min": sharpes[0],
            "random_median": st.median(sharpes),
            "random_max": sharpes[-1],
            "beats": beat,
            "percentile": beat / len(sharpes) * 100,
            "z_score": (real.metrics.sharpe - st.mean(sharpes)) / sd if sd else None,
            "random_sharpes": sharpes,
        }
        summary[name] = block
        print(f"\n### {name} {start}..{end}")
        print(f"  S003 Sharpe          {real.metrics.sharpe:>6.2f}")
        print(
            f"  random n={len(sharpes)}  mean {st.mean(sharpes):>6.2f}  sd {sd:>5.2f}  "
            f"min {sharpes[0]:>5.2f}  med {st.median(sharpes):>5.2f}  max {sharpes[-1]:>5.2f}"
        )
        print(
            f"  beats {beat}/{len(sharpes)} = {block['percentile']:.0f}th pct   "
            f"z = {block['z_score']:+.2f}"
        )

    path = manifest.record(
        label="random-entry-control",
        spec={"seeds": args.seeds, "entry_probability": MATCHED_ENTRY_PROB},
        extra={"script": __name__, "summary": summary},
        data_dir=DATA,
    )
    print(f"\nrecorded -> {path.relative_to(path.parents[2])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
