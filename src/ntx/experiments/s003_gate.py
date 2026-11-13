"""S003 gate verification: IS vs OOS, funding applied, lineage recorded.

Reproduces the numbers quoted in docs/strategies/S003-donchian-breakout.md §3-4.

    python -m ntx.experiments.s003_gate
    python -m ntx.experiments.s003_gate --live-config

`--live-config` runs the same strategy at the configuration that will actually
trade: locally provided equity and instruments, and 5 bps slippage. The operator
must verify the private inputs are current. The default run is
the portfolio-scale reference; the live-config run is the go-live evidence
(T-01, review 2026-09-18).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from ntx.backtest import funding as fnd
from ntx.backtest.runner import BacktestSpec
from ntx.config.universe import UNIVERSE
from ntx.experiments._common import DATA, IS_WINDOW, OOS_WINDOW, evaluate, record
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.registry import DEPLOYED

CONFIG = DEPLOYED["S003"]
GATE = {"sharpe": 0.40, "maxdd": 40.0, "pf": 1.15, "trades": 200}

# Account-specific inputs are kept locally and are required for this optional run.
PRIVATE_CONFIG = Path(__file__).resolve().parents[3] / "docs/private/s003-live-config.json"
# docs/09 §3: 5 bps for alts. Kept for BTC and BNB too, where it is conservative --
# they are the most liquid names here, so this understates nothing.
LIVE_SLIPPAGE_BPS = 5.0


def load_live_config(path: Path) -> tuple[float, tuple[str, ...]]:
    """Load locally measured inputs; fail closed if missing or invalid."""
    data = json.loads(path.read_text())
    equity = data["equity_usd"]
    assets = data["assets"]
    known = {a.base for a in UNIVERSE}
    if (
        isinstance(equity, bool)
        or not isinstance(equity, (int, float))
        or not math.isfinite(equity)
        or equity <= 0
        or not isinstance(assets, list)
        or not assets
        or any(not isinstance(a, str) or a not in known for a in assets)
        or len(set(assets)) != len(assets)
    ):
        raise ValueError("invalid private live gate configuration")
    return float(equity), tuple(assets)


def __getattr__(name: str):
    """Resolve private inputs only when a dependent study requests them."""
    if name in {"LIVE_EQUITY_USD", "LIVE_ASSETS"}:
        equity, assets = load_live_config(PRIVATE_CONFIG)
        return equity if name == "LIVE_EQUITY_USD" else assets
    raise AttributeError(name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S003 gate")
    ap.add_argument(
        "--live-config",
        action="store_true",
        help="private local equity and instruments, with 5 bps slippage",
    )
    args = ap.parse_args(argv)
    live = args.live_config
    equity, assets = (
        load_live_config(PRIVATE_CONFIG) if live else (10_000.0, tuple(a.base for a in UNIVERSE))
    )
    slippage = LIVE_SLIPPAGE_BPS if live else 2.0
    tag = "gate-live" if live else "gate"

    series = fnd.load_funding_series(DATA / "raw")
    print(
        f"S003 Donchian 55/20 sl2.0, 4h, risk 0.5%, {slippage:.0f}bps slippage, "
        f"funding applied, ${equity:,.2f} on {len(assets)} assets\n"
    )
    print(
        f"{'window':<13}{'trades':>7}{'mtm%':>8}{'PF raw':>8}{'PF+fund':>9}"
        f"{'maxDD%':>8}{'Sharpe':>8}{'funding%':>10}"
    )
    print("-" * 71)
    results = {}
    for name, (start, end) in (("IS", IS_WINDOW), ("OOS", OOS_WINDOW)):
        spec = BacktestSpec(
            strategy_cls=DonchianBreakoutStrategy,
            config_cls=DonchianBreakoutConfig,
            config_kwargs=CONFIG,
            timeframe="4h",
            start=start,
            end=end,
            starting_equity_usd=equity,
            slippage_bps_per_side=slippage,
            assets=assets,
        )
        out = evaluate(spec, series)
        m = out.metrics
        results[name] = out
        path = record(f"s003-{tag}-{name}", spec, out, script=__name__, window=[start, end])
        print(
            f"{name:<13}{m.trades:>7}{m.return_pct:>8.0f}{m.profit_factor:>8.2f}"
            f"{out.pf_after_funding:>9.2f}{m.max_drawdown_pct:>8.1f}{m.sharpe:>8.2f}"
            f"{out.funding_impact.pct_of_gross_pnl:>9.1f}%"
        )
        print(f"             -> {path.relative_to(path.parents[2])}")

    o = results["OOS"]
    checks = {
        "sharpe >= 0.40": o.metrics.sharpe >= GATE["sharpe"],
        "maxDD <= 40%": o.metrics.max_drawdown_pct <= GATE["maxdd"],
        "PF (after funding) >= 1.15": o.pf_after_funding >= GATE["pf"],
        "trades >= 200": o.metrics.trades >= GATE["trades"],
    }
    print("\nGATE (out-of-sample):")
    for k, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {k}")
    verdict = all(checks.values())
    print(f"\nVERDICT: {'PASS' if verdict else 'FAIL'}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
