"""Staged parameter sweep with a plateau requirement.

The full S001 grid is ~10^7 combinations; sweeping it would guarantee finding
noise. So sweeps run in **stages**, each fixing the winner of the previous one
(docs/09-backtest-methodology.md §5).

The plateau rule is what kills most overfit: a configuration is accepted only if
its immediate grid neighbours retain >=80% of its Sharpe. An isolated peak is
rejected **even when its number is the best in the sweep**.
"""

from __future__ import annotations

import itertools
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ntx.backtest.metrics import Metrics, compute
from ntx.backtest.runner import BacktestSpec, run

log = logging.getLogger(__name__)

# In-sample window. OOS (2024-01 onward) is reserved for final verification of a
# single chosen candidate (R37, F017).
IS_START, IS_END = "2020-01-01", "2023-12-31"


@dataclass
class SweepRow:
    params: dict
    metrics: dict
    fee_share_of_gross_pct: float
    red_flags: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def sharpe(self) -> float:
        return self.metrics.get("sharpe", 0.0)


def _fee_share(m: Metrics) -> float:
    gross = m.net_pnl + m.fees_paid
    return (m.fees_paid / abs(gross) * 100.0) if gross else float("nan")


def run_grid(
    *,
    strategy_cls,
    config_cls,
    grid: dict[str, list],
    data_dir: Path,
    timeframe: str,
    assets: tuple[str, ...] | None = None,
    start: str = IS_START,
    end: str = IS_END,
    starting_equity_usd: float = 10_000.0,
    valid: callable | None = None,
) -> list[SweepRow]:
    """Run every combination in `grid`. `valid(params)` may reject combinations."""
    keys = list(grid)
    rows: list[SweepRow] = []
    combos = [dict(zip(keys, v, strict=True)) for v in itertools.product(*grid.values())]
    if valid:
        combos = [c for c in combos if valid(c)]
    log.info("sweep: %d combinations on %s, %s..%s", len(combos), timeframe, start, end)

    for i, params in enumerate(combos, 1):
        spec = BacktestSpec(
            strategy_cls=strategy_cls,
            config_cls=config_cls,
            config_kwargs=dict(params),
            timeframe=timeframe,
            start=start,
            end=end,
            starting_equity_usd=starting_equity_usd,
            **({"assets": assets} if assets else {}),
        )
        t0 = time.time()
        try:
            res = run(spec, data_dir)
            m = compute(
                res.positions_report,
                res.equity_curve,
                starting_equity_usd,
                res.bars,
                timeframe=timeframe,
            )
        except Exception as e:  # a failed cell must not kill the sweep
            log.warning("combo %s failed: %s", params, e)
            continue
        row = SweepRow(
            params={"timeframe": timeframe, **params},
            metrics=asdict(m),
            fee_share_of_gross_pct=_fee_share(m),
            red_flags=m.red_flags(),
            seconds=time.time() - t0,
        )
        rows.append(row)
        log.info(
            "[%d/%d] %s -> trades=%d ret=%.0f%% Sharpe=%.2f maxDD=%.0f%% fee/gross=%.0f%% (%.0fs)",
            i,
            len(combos),
            params,
            m.trades,
            m.return_pct,
            m.sharpe,
            m.max_drawdown_pct,
            row.fee_share_of_gross_pct,
            row.seconds,
        )
    return rows


def plateau_check(
    rows: list[SweepRow], axes: tuple[str, ...], *, retain: float = 0.80
) -> tuple[SweepRow | None, list[SweepRow], str]:
    """Pick the best row that sits on a plateau rather than an isolated peak.

    Neighbours differ by one grid step on exactly ONE of `axes`, with **every
    other parameter held equal**. Keying the index on `axes` alone silently
    collapsed rows that differed on a non-axis parameter -- an EMA cell was
    compared against SMA neighbours, mixing two different surfaces (F018).

    A candidate is accepted only if every neighbour retains `retain` of its
    Sharpe. Rejecting a lone peak even when it is the highest number in the
    sweep is the single most effective anti-overfit rule available.
    """
    if not rows:
        return None, [], "no rows"

    def full_key(r: SweepRow) -> tuple:
        return tuple(sorted((k, str(v)) for k, v in r.params.items()))

    values = {ax: sorted({r.params[ax] for r in rows}) for ax in axes}
    index = {full_key(r): r for r in rows}
    notes: list[str] = []

    for cand in sorted(rows, key=lambda r: r.sharpe, reverse=True):
        label = {ax: cand.params[ax] for ax in axes}
        neighbours: list[SweepRow] = []
        for ax in axes:
            vals = values[ax]
            pos = vals.index(cand.params[ax])
            for step in (-1, 1):
                j = pos + step
                if not 0 <= j < len(vals):
                    continue
                probe = dict(cand.params)
                probe[ax] = vals[j]
                key = tuple(sorted((k, str(v)) for k, v in probe.items()))
                n = index.get(key)
                if n is not None:
                    neighbours.append(n)
        if cand.sharpe <= 0:
            notes.append(f"{label}: Sharpe {cand.sharpe:.2f} <= 0")
            continue
        if not neighbours:
            notes.append(f"{label}: no in-grid neighbours, plateau unassessable")
            continue
        worst = min(n.sharpe for n in neighbours)
        if worst >= cand.sharpe * retain:
            return (
                cand,
                neighbours,
                (
                    f"plateau ok: {len(neighbours)} neighbours, worst Sharpe {worst:.2f} "
                    f">= {retain:.0%} of {cand.sharpe:.2f}"
                ),
            )
        notes.append(
            f"{label}: REJECTED isolated peak (Sharpe {cand.sharpe:.2f}, "
            f"worst of {len(neighbours)} neighbours {worst:.2f} "
            f"= {worst / cand.sharpe:.0%})"
        )
    return None, [], " | ".join(notes[:8])


def save(rows: list[SweepRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(r) for r in rows], indent=2, default=str))


def table(rows: list[SweepRow], top: int = 15) -> str:
    head = (
        f"{'tf':>4} {'ma':>4} {'fast':>5} {'slow':>5} {'trades':>7} {'ret%':>8} "
        f"{'win%':>6} {'PF':>5} {'maxDD%':>7} {'Sharpe':>7} {'fee/gr%':>8}"
    )
    out = [head, "-" * len(head)]
    for r in sorted(rows, key=lambda x: x.sharpe, reverse=True)[:top]:
        p, m = r.params, r.metrics
        out.append(
            f"{p.get('timeframe', ''):>4} {p.get('ma_type', ''):>4} "
            f"{p.get('fast_period', ''):>5} {p.get('slow_period', ''):>5} "
            f"{m['trades']:>7} {m['return_pct']:>8.0f} {m['win_rate'] * 100:>6.1f} "
            f"{m['profit_factor']:>5.2f} {m['max_drawdown_pct']:>7.1f} "
            f"{m['sharpe']:>7.2f} {r.fee_share_of_gross_pct:>8.0f}"
        )
    return "\n".join(out)
