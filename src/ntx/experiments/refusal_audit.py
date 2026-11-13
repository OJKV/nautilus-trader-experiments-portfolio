"""How often does the simulated venue refuse S003's orders? (F035 follow-up)

F035 added rejection handling. It moved the S003 gate slightly (OOS 806 -> 808
trades), which means refusals happen in backtests too and previously left
phantom position state behind. This counts them.

    python -m ntx.experiments.refusal_audit
"""

from __future__ import annotations

from ntx.backtest.runner import BacktestSpec, run
from ntx.experiments._common import DATA, IS_WINDOW, OOS_WINDOW
from ntx.experiments.s003_gate import CONFIG
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy


def main() -> int:
    for name, (start, end) in (("IS", IS_WINDOW), ("OOS", OOS_WINDOW)):
        spec = BacktestSpec(
            strategy_cls=DonchianBreakoutStrategy,
            config_cls=DonchianBreakoutConfig,
            config_kwargs=CONFIG,
            timeframe="4h",
            start=start,
            end=end,
            starting_equity_usd=10_000.0,
            slippage_bps_per_side=2.0,
        )
        c = run(spec, DATA).strategy_counters
        print(
            f"{name}: submitted={c['entries_submitted']} entries_refused={c['entries_refused']} "
            f"stops_refused={c['stops_refused']} unprotected_events={c['unprotected_events']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
