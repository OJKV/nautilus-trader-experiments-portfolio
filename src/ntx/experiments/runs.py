"""List recorded backtest runs -- the review entry point.

python -m ntx.experiments.runs
"""

from __future__ import annotations

from ntx.backtest.manifest import RUNS_DIR, index


def main() -> int:
    df = index()
    if df.empty:
        print(f"no runs recorded under {RUNS_DIR}")
        return 0
    with __import__("pandas").option_context("display.width", 200, "display.max_columns", 20):
        print(df.to_string(index=False))
    print(
        f"\n{len(df)} runs. Each directory holds manifest.json (config, source "
        f"hash, data hash, environment), metrics.json, trades.csv, equity.csv."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
