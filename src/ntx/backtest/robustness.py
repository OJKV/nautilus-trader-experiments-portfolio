"""Robustness suite (docs/09-backtest-methodology.md §7).

A single headline Sharpe says almost nothing. These checks ask the questions
that decide whether a number is an edge or an artefact:

  regime split      -- does it only work in one market condition?
  per-asset         -- is it one lucky instrument carrying the whole result?
  start sensitivity -- does shifting the start date by a quarter change the answer?
  Monte Carlo       -- how bad is the drawdown in unlucky orderings of the same trades?
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ntx.backtest.metrics import Metrics, compute, money_to_float
from ntx.backtest.runner import BacktestSpec, run

# Crypto regimes, chosen from BTC's own path rather than fitted to results.
REGIMES: tuple[tuple[str, str, str], ...] = (
    ("2020 covid crash + recovery", "2020-01-01", "2020-12-31"),
    ("2021 bull", "2021-01-01", "2021-12-31"),
    ("2022 bear", "2022-01-01", "2022-12-31"),
    ("2023 chop / recovery", "2023-01-01", "2023-12-31"),
)


@dataclass
class Slice:
    label: str
    metrics: Metrics
    start: str
    end: str

    def row(self) -> str:
        m = self.metrics
        return (
            f"{self.label:<30}{m.trades:>7}{m.return_pct:>9.0f}"
            f"{m.realized_return_pct:>10.0f}{m.unrealized_at_end_pct:>7.0f}"
            f"{m.win_rate * 100:>7.1f}{m.profit_factor:>6.2f}"
            f"{m.max_drawdown_pct:>8.1f}{m.sharpe:>8.2f}"
        )


def _run(
    spec_kwargs: dict,
    data_dir: Path,
    *,
    start: str,
    end: str,
    assets: tuple[str, ...] | None = None,
) -> tuple[Metrics, pd.DataFrame]:
    kw = dict(spec_kwargs)
    kw.update(start=start, end=end)
    if assets is not None:
        kw["assets"] = assets
    spec = BacktestSpec(**kw)
    res = run(spec, data_dir)
    m = compute(
        res.positions_report,
        res.equity_curve,
        spec.starting_equity_usd,
        res.bars,
        timeframe=spec.timeframe,
    )
    return m, res.positions_report


def regime_split(spec_kwargs: dict, data_dir: Path) -> list[Slice]:
    out = []
    for label, start, end in REGIMES:
        try:
            m, _ = _run(spec_kwargs, data_dir, start=start, end=end)
        except Exception:  # a regime with no data is not a failure
            continue
        out.append(Slice(label, m, start, end))
    return out


def per_asset(spec_kwargs: dict, data_dir: Path, assets: tuple[str, ...]) -> list[Slice]:
    """One backtest per asset. §7 requires positive on >= 6 of 10."""
    out = []
    for a in assets:
        try:
            m, _ = _run(
                spec_kwargs,
                data_dir,
                start=spec_kwargs["start"],
                end=spec_kwargs["end"],
                assets=(a,),
            )
        except Exception:
            continue
        out.append(Slice(a, m, spec_kwargs["start"], spec_kwargs["end"]))
    return out


def start_sensitivity(
    spec_kwargs: dict, data_dir: Path, offsets_months: tuple[int, ...] = (-3, 0, 3, 6)
) -> list[Slice]:
    """Shift the start date. A result that depends on where the window begins is
    a result about that window, not about the strategy."""
    base = pd.Timestamp(spec_kwargs["start"])
    out = []
    for off in offsets_months:
        start = (base + pd.DateOffset(months=off)).strftime("%Y-%m-%d")
        try:
            m, _ = _run(spec_kwargs, data_dir, start=start, end=spec_kwargs["end"])
        except Exception:
            continue
        out.append(Slice(f"start {start} ({off:+d}m)", m, start, spec_kwargs["end"]))
    return out


def monte_carlo(
    positions: pd.DataFrame, starting_equity: float, *, n: int = 2000, seed: int = 42
) -> dict:
    """Two distinct resampling tests over the realised trade set.

    **Permutation** (shuffle without replacement) answers *sequence risk*: the
    same trades in a different order. It cannot change the final equity -- the
    sum of a permuted set is identical -- so reporting final-equity percentiles
    from it is vacuous, which an earlier version of this function did (F022).
    Permutation speaks only to drawdown.

    **Bootstrap** (sample WITH replacement) answers *outcome risk*: what if the
    trade mix had been luckier or unluckier. This one does move the final equity
    and is the basis for P(loss).

    Both use additive PnL and therefore ignore compounding, so they understate
    the spread of a strategy whose position size scales with equity. They bound
    sequence and sampling risk, not total risk.
    """
    pnl = money_to_float(positions["realized_pnl"]).dropna().to_numpy()
    if len(pnl) < 10:
        return {}
    rng = np.random.default_rng(seed)

    def max_dd(path: np.ndarray) -> float:
        peak = np.maximum.accumulate(np.concatenate([[starting_equity], path]))
        return float(np.max((peak[1:] - path) / peak[1:]) * 100.0)

    perm_dd = np.array(
        [max_dd(starting_equity + np.cumsum(rng.permutation(pnl))) for _ in range(n)]
    )
    boot_finals = np.empty(n)
    boot_dd = np.empty(n)
    for i in range(n):
        sample = rng.choice(pnl, size=len(pnl), replace=True)
        path = starting_equity + np.cumsum(sample)
        boot_finals[i] = path[-1]
        boot_dd[i] = max_dd(path)

    return {
        "trades": len(pnl),
        "realised_final_equity": float(starting_equity + pnl.sum()),
        # sequence risk -- same trades, different order
        "perm_median_maxdd_pct": float(np.median(perm_dd)),
        "perm_p95_maxdd_pct": float(np.percentile(perm_dd, 95)),
        "perm_worst_maxdd_pct": float(perm_dd.max()),
        # outcome risk -- resampled trade mix
        "boot_p5_final_equity": float(np.percentile(boot_finals, 5)),
        "boot_median_final_equity": float(np.median(boot_finals)),
        "boot_p95_maxdd_pct": float(np.percentile(boot_dd, 95)),
        "boot_prob_loss": float((boot_finals < starting_equity).mean()),
    }


def header() -> str:
    """Column labels matching `Slice.row()`.

    `mtm%` is the mark-to-market return from the equity curve; `realis%` is
    realised PnL over starting equity; `unre%` is the share of final equity
    still riding on OPEN positions -- a high value means the result depends on
    where the window was cut (F021).
    """
    return (
        f"{'slice':<30}{'trades':>7}{'mtm%':>9}{'realis%':>10}{'unre%':>7}"
        f"{'win%':>7}{'PF':>6}{'maxDD%':>8}{'Sharpe':>8}"
    )
