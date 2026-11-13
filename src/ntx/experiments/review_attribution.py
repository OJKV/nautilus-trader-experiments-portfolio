"""Which review fix moved the S003 numbers? (R40)

After the 2026-09-17 review fixes, IS Sharpe fell 0.97 -> 0.85. This reruns IS
and OOS with H2 (open notional at mark) reverted to entry-price notional, so the
H2 share of the change is isolated. The remainder is H1 (flip ordering) and C4
(equity sampled at one timestamp).

    python -m ntx.experiments.review_attribution
"""

from __future__ import annotations

from ntx.backtest import funding as fnd
from ntx.backtest.runner import BacktestSpec
from ntx.experiments._common import DATA, IS_WINDOW, OOS_WINDOW, evaluate
from ntx.experiments.s003_gate import CONFIG
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy


class EntryNotional(DonchianBreakoutStrategy):
    def _open_notional_usd(self) -> float:  # pre-H2 behaviour
        return sum(abs(float(s.base_units)) * s.entry_price for s in self.states.values())


class OldFlip(DonchianBreakoutStrategy):
    """Pre-H1: re-enter at once; on_position_closed pops whatever state exists."""

    wiped_fresh = 0

    def _flip(self, inst_id, direction, bar) -> None:
        self._enter(inst_id, direction, bar)

    def on_position_closed(self, event) -> None:
        st = self.states.pop(event.instrument_id, None)
        if st is not None and st.meta.get("filled_qty", 0.0) == 0:
            OldFlip.wiped_fresh += 1
        self._cancel_protective_stop(event.instrument_id)
        pending = self._pending_entry.pop(event.instrument_id, None)
        if pending is not None:
            self._enter(event.instrument_id, *pending)


def main() -> int:
    series = fnd.load_funding_series(DATA / "raw")
    for label, cls in (
        ("current", DonchianBreakoutStrategy),
        ("H2 reverted", EntryNotional),
        ("H1 reverted", OldFlip),
    ):
        for name, (start, end) in (("IS", IS_WINDOW), ("OOS", OOS_WINDOW)):
            spec = BacktestSpec(
                strategy_cls=cls,
                config_cls=DonchianBreakoutConfig,
                config_kwargs=CONFIG,
                timeframe="4h",
                start=start,
                end=end,
                starting_equity_usd=10_000.0,
                slippage_bps_per_side=2.0,
            )
            m = evaluate(spec, series).metrics
            print(
                f"{label:<12} {name:<4} trades={m.trades} mtm={m.return_pct:.0f}% "
                f"maxDD={m.max_drawdown_pct:.1f}% sharpe={m.sharpe:.2f}"
                + (f" fresh_states_wiped={OldFlip.wiped_fresh}" if cls is OldFlip else "")
            )
            OldFlip.wiped_fresh = 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
