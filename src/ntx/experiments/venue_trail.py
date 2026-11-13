"""Should the stop AT THE VENUE follow the trailing stop? (B-08 / G8)

The safety argument is strong: today the venue stop sits at its initial 2xATR
level for the life of the trade, so a position that is +15% gives all of it back
if the node is down when price turns. Making the venue stop trail closes that
gap.

The cost is that a venue stop can be hit **intrabar**, by a wick, while the
Python trailing stop is only evaluated on the bar close. This run measures that
cost instead of arguing about it.

`tsl_sync_min_price_pct` is how much tighter the trailing level must be before
the stop is moved (every move is a cancel + place at the venue). 0 disables the
sync, which is the behaviour every recorded gate run so far used.

T3-05 (R-007): the first table compared an "off" row whose run had latched a
KILL (IS maxDD 20.3%) with ON rows that never KILLed -- a truncated run against
full ones. `--guards-off` disables both loss guards so every row trades the
whole window, and the KILL column shows whether a guard ended a run early.

    python -m ntx.experiments.venue_trail [--guards-off] [--thresholds 0,0.25]
"""

from __future__ import annotations

import argparse

from ntx.backtest import funding as fnd
from ntx.backtest.runner import BacktestSpec
from ntx.experiments._common import DATA, IS_WINDOW, OOS_WINDOW, evaluate, record
from ntx.experiments.s003_gate import LIVE_ASSETS, LIVE_SLIPPAGE_BPS
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.registry import DEPLOYED
from ntx.strategies.signal import ExitReason

THRESHOLDS = (0.0, 0.25, 1.0, 2.0)


def _spec(
    pct: float, window: tuple[str, str], equity: float, guards_off: bool = False
) -> BacktestSpec:
    cfg = dict(DEPLOYED["S003"])
    cfg["tsl_sync_min_price_pct"] = pct
    if guards_off:
        # 0 disables a guard (guards.py); comparable rows, not a deployable config.
        cfg["daily_loss_limit_equity_pct"] = 0.0
        cfg["max_drawdown_equity_pct"] = 0.0
    return BacktestSpec(
        strategy_cls=DonchianBreakoutStrategy,
        config_cls=DonchianBreakoutConfig,
        config_kwargs=cfg,
        timeframe="4h",
        start=window[0],
        end=window[1],
        starting_equity_usd=equity,
        slippage_bps_per_side=LIVE_SLIPPAGE_BPS,
        assets=LIVE_ASSETS,
    )


def _killed(counters: dict) -> str:
    """Did a loss guard end the run early? A KILL truncates the row (T3-05)."""
    exits = counters.get("exits") or {}
    skips = counters.get("entry_skips") or {}
    return "yes" if exits.get(str(ExitReason.KILL_SWITCH)) or skips.get("killed") else "no"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="does a trailing VENUE stop pay for itself?")
    ap.add_argument("--equity", type=float, default=100.0)
    ap.add_argument("--guards-off", action="store_true", help="disable both loss guards")
    ap.add_argument("--thresholds", default=",".join(f"{t:g}" for t in THRESHOLDS))
    args = ap.parse_args(argv)
    thresholds = [float(t) for t in args.thresholds.split(",")]
    suffix = "-guardsoff" if args.guards_off else ""
    series = fnd.load_funding_series(DATA / "raw")

    print(
        f"S003 55/20, 4h, risk 0.5%, 5 bps, funding applied, ${args.equity:,.0f}, "
        f"{len(LIVE_ASSETS)} live assets\n"
        "tsl_sync = how much tighter the trailing level must be to move the venue stop\n"
        f"loss guards: {'OFF (comparable rows)' if args.guards_off else 'as deployed'}\n"
    )
    for name, window in (("IS", IS_WINDOW), ("OOS", OOS_WINDOW)):
        print(f"=== {name} {window[0]}..{window[1]} ===")
        print(
            f"{'tsl_sync':<12}{'trades':>8}{'ret%':>9}{'PF+fund':>9}{'maxDD%':>9}"
            f"{'Sharpe':>8}{'KILL':>6}"
        )
        print("-" * 61)
        for pct in thresholds:
            spec = _spec(pct, window, args.equity, args.guards_off)
            out = evaluate(spec, series)
            m = out.metrics
            label = "off" if pct == 0 else f"{pct:g}%"
            record(
                f"s003-venuetrail-{label.replace('%', 'pct')}{suffix}-{name}",
                spec,
                out,
                script=__name__,
                window=list(window),
            )
            print(
                f"{label:<12}{m.trades:>8}{m.return_pct:>9.0f}{out.pf_after_funding:>9.2f}"
                f"{m.max_drawdown_pct:>9.1f}{m.sharpe:>8.2f}{_killed(out.counters):>6}"
            )
        print()
    print(
        "A venue stop can be hit intrabar; the Python trailing stop only acts on a\n"
        "bar close. The difference between 'off' and the rest is what that costs --\n"
        "against the unmeasurable benefit of being protected while the node is down."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
