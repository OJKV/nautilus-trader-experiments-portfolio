"""Does attaching the stop to the entry change the backtest? (G7 / OPS-4)

Fill-then-attach only exits at a bar CLOSE, because the strategy evaluates exits
on bar close. A venue stop attached to the entry can fire **intrabar**, on the
bar's low/high. That is more realistic -- and a different execution model -- so
the validated S003 numbers must be re-measured before it becomes the default.

    python -m ntx.experiments.attached_stop_compare
"""

from __future__ import annotations

from ntx.backtest import funding as fnd
from ntx.backtest.runner import BacktestSpec
from ntx.experiments._common import DATA, IS_WINDOW, OOS_WINDOW, evaluate, record
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.registry import DEPLOYED


def main() -> int:
    series = fnd.load_funding_series(DATA / "raw")
    print(
        f"{'window':<6}{'stop':<10}{'trades':>8}{'mtm%':>7}{'PF+fund':>9}{'maxDD%':>8}{'Sharpe':>8}"
    )
    for name, (start, end) in (("IS", IS_WINDOW), ("OOS", OOS_WINDOW)):
        for label, attached in (("at close", False), ("attached", True)):
            spec = BacktestSpec(
                strategy_cls=DonchianBreakoutStrategy,
                config_cls=DonchianBreakoutConfig,
                config_kwargs={**DEPLOYED["S003"], "venue_attaches_stop": attached},
                timeframe="4h",
                start=start,
                end=end,
                starting_equity_usd=10_000.0,
                slippage_bps_per_side=2.0,
            )
            out = evaluate(spec, series)
            m = out.metrics
            record(f"s003-attachstop-{name}-{'on' if attached else 'off'}", spec, out,
                   script=__name__)  # fmt: skip
            print(f"{name:<6}{label:<10}{m.trades:>8}{m.return_pct:>7.0f}"
                  f"{out.pf_after_funding:>9.2f}{m.max_drawdown_pct:>8.1f}{m.sharpe:>8.2f}",
                  flush=True)  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
