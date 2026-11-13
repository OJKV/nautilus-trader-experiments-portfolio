"""Shared plumbing for experiments: run a spec, apply funding, record lineage."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ntx.backtest import funding as fnd
from ntx.backtest import manifest
from ntx.backtest.metrics import Metrics, compute, money_to_float
from ntx.backtest.runner import BacktestSpec, run
from ntx.config.universe import UNIVERSE
from ntx.data.read import open_catalog, read_instrument

DATA = Path(__file__).resolve().parents[3] / "data"

IS_WINDOW = ("2020-01-01", "2023-12-31")
OOS_WINDOW = ("2024-01-01", "2026-08-31")


@dataclass
class Outcome:
    metrics: Metrics
    positions: pd.DataFrame
    equity: pd.Series
    funding: pd.Series
    funding_impact: fnd.FundingImpact
    pf_after_funding: float
    realised_after_funding: float
    # Why entries did NOT happen: rejects, guard skips, cap hits. Without these a
    # knob that silently does nothing looks the same as a genuine null result
    # (R40), and they never reached a manifest before (review T-09).
    counters: dict = field(default_factory=dict)


def multipliers() -> dict[str, float]:
    cat = open_catalog(DATA)
    return {a.nautilus_id: float(read_instrument(cat, a.nautilus_id).multiplier) for a in UNIVERSE}


def evaluate(spec: BacktestSpec, funding_series: dict[str, pd.Series] | None = None) -> Outcome:
    """Run a backtest and apply funding (R7). Funding is never optional."""
    res = run(spec, DATA)
    m = compute(
        res.positions_report,
        res.equity_curve,
        spec.starting_equity_usd,
        res.bars,
        timeframe=spec.timeframe,
    )
    series = funding_series if funding_series is not None else fnd.load_funding_series(DATA / "raw")
    fs, impact = fnd.apply(res.positions_report, series, multipliers())

    pnl = money_to_float(res.positions_report["realized_pnl"]).dropna()
    adj = pnl.add(fs, fill_value=0.0)
    pf = (adj[adj > 0].sum() / -adj[adj < 0].sum()) if (adj < 0).any() else float("inf")
    return Outcome(
        m,
        res.positions_report,
        res.equity_curve,
        fs,
        impact,
        float(pf),
        float(adj.sum()),
        dict(res.strategy_counters or {}),
    )


def record(label: str, spec: BacktestSpec, out: Outcome, **extra) -> Path:
    return manifest.record(
        label=label,
        spec=spec,
        metrics=out.metrics,
        positions=out.positions,
        equity=out.equity,
        funding=out.funding,
        data_dir=DATA,
        extra={
            "funding_impact": out.funding_impact,
            "pf_after_funding": out.pf_after_funding,
            "realised_after_funding": out.realised_after_funding,
            # R40/T-09: the rejects and guard skips that shaped this result.
            "strategy_counters": out.counters,
            **extra,
        },
    )
