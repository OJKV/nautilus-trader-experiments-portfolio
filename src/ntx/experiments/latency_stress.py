"""S003 latency stress (docs/09 §7, R8c): every decision filled one bar late.

    python -m ntx.experiments.latency_stress

Live-config S003 (the gate's `--live-config` inputs) with `decision_delay_bars`
0 and 1, IS and OOS. Delay 0 must reproduce the gate exactly (R67); delay 1
asks whether the edge survives a whole 4h bar of execution lag on every entry
and every signal exit. Stops are unaffected (they trigger intrabar).
"""

from __future__ import annotations

from ntx.backtest import funding as fnd
from ntx.backtest.runner import BacktestSpec
from ntx.experiments._common import DATA, IS_WINDOW, OOS_WINDOW, evaluate, record
from ntx.experiments.s003_gate import CONFIG, LIVE_ASSETS, LIVE_EQUITY_USD, LIVE_SLIPPAGE_BPS
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy


def main() -> int:
    series = fnd.load_funding_series(DATA / "raw")
    print(f"{'window':<6}{'delay':>6}{'trades':>8}{'PF+fund':>9}{'maxDD%':>8}{'Sharpe':>8}")
    for name, (start, end) in (("IS", IS_WINDOW), ("OOS", OOS_WINDOW)):
        for delay in (0, 1):
            spec = BacktestSpec(
                strategy_cls=DonchianBreakoutStrategy,
                config_cls=DonchianBreakoutConfig,
                config_kwargs={**CONFIG, "decision_delay_bars": delay},
                timeframe="4h",
                start=start,
                end=end,
                starting_equity_usd=LIVE_EQUITY_USD,
                slippage_bps_per_side=LIVE_SLIPPAGE_BPS,
                assets=LIVE_ASSETS,
            )
            out = evaluate(spec, series)
            m = out.metrics
            record(f"s003-latency-{name}-d{delay}", spec, out, script=__name__)
            print(
                f"{name:<6}{delay:>6}{m.trades:>8}{out.pf_after_funding:>9.2f}"
                f"{m.max_drawdown_pct:>8.1f}{m.sharpe:>8.2f}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
