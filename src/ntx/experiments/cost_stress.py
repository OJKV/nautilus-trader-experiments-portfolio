"""S003 cost stress (docs/09 §7): does the edge survive worse execution?

Grid: slippage 2 / 5 bps per side x fee multiplier 1 / 2, IS and OOS, funding
applied. Every cell is recorded under reports/runs (R42). Reproduces the cost
lines in docs/strategies/S003-donchian-breakout.md.

    python -m ntx.experiments.cost_stress
"""

from __future__ import annotations

from ntx.backtest import funding as fnd
from ntx.backtest.runner import BacktestSpec
from ntx.experiments._common import DATA, IS_WINDOW, OOS_WINDOW, evaluate, record
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.registry import DEPLOYED

CELLS = ((2.0, 1.0), (5.0, 1.0), (2.0, 2.0), (5.0, 2.0))


def main() -> int:
    series = fnd.load_funding_series(DATA / "raw")
    print(f"{'window':<6}{'slip':>6}{'fee x':>7}{'trades':>8}{'mtm%':>7}{'PF+fund':>9}"
          f"{'maxDD%':>8}{'Sharpe':>8}")  # fmt: skip
    for name, (start, end) in (("IS", IS_WINDOW), ("OOS", OOS_WINDOW)):
        for slip, mult in CELLS:
            spec = BacktestSpec(
                strategy_cls=DonchianBreakoutStrategy,
                config_cls=DonchianBreakoutConfig,
                config_kwargs=DEPLOYED["S003"],
                timeframe="4h",
                start=start,
                end=end,
                starting_equity_usd=10_000.0,
                slippage_bps_per_side=slip,
                fee_multiplier=mult,
            )
            out = evaluate(spec, series)
            m = out.metrics
            record(f"s003-cost-{name}-slip{slip:g}-fee{mult:g}", spec, out, script=__name__)
            print(f"{name:<6}{slip:>6g}{mult:>7g}{m.trades:>8}{m.return_pct:>7.0f}"
                  f"{out.pf_after_funding:>9.2f}{m.max_drawdown_pct:>8.1f}{m.sharpe:>8.2f}",
                  flush=True)  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
