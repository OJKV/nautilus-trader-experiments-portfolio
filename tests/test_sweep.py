"""Sweep and plateau-check tests.

The plateau rule is the project's main anti-overfit guard, and it shipped broken
once (F018) -- compared EMA cells against SMA neighbours because the neighbour
index was keyed on the plateau axes alone. A broken guard is worse than no guard
because it grants false confidence, so it gets tested properly.
"""

from __future__ import annotations

import math

import pytest

from ntx.backtest.sweep import SweepRow, plateau_check, table


def _row(fast: int, slow: int, sharpe: float, ma: str = "EMA", **extra) -> SweepRow:
    return SweepRow(
        params={
            "timeframe": "4h",
            "ma_type": ma,
            "fast_period": fast,
            "slow_period": slow,
            **extra,
        },
        metrics={
            "sharpe": sharpe,
            "trades": 500,
            "return_pct": 100.0,
            "win_rate": 0.42,
            "profit_factor": 1.3,
            "max_drawdown_pct": 30.0,
        },
        fee_share_of_gross_pct=12.0,
    )


AXES = ("fast_period", "slow_period")


def test_no_rows():
    best, _nb, note = plateau_check([], AXES)
    assert best is None and note == "no rows"


def test_smooth_surface_accepts_the_peak():
    """A genuine plateau: the best cell's neighbours are all close behind."""
    rows = [
        _row(8, 100, 0.90),
        _row(12, 100, 1.00),
        _row(20, 100, 0.92),
        _row(8, 150, 0.95),
        _row(12, 150, 1.05),
        _row(20, 150, 0.96),
        _row(8, 200, 0.88),
        _row(12, 200, 0.98),
        _row(20, 200, 0.90),
    ]
    best, nb, note = plateau_check(rows, AXES)
    assert best is not None
    assert (best.params["fast_period"], best.params["slow_period"]) == (12, 150)
    assert "plateau ok" in note
    assert len(nb) == 4  # up/down on each of two axes


def test_isolated_peak_is_rejected():
    """The whole point: a lone spike never wins, however good its number.

    `plateau_check` answers only "which is the best cell that sits on a
    plateau". It deliberately applies **no** absolute threshold -- the go-live
    gate (S001 §8) does that separately. So here the 3.00 spike is rejected and
    the walk settles on a modest cell that is genuinely flat.
    """
    rows = [
        _row(8, 100, 0.20),
        _row(12, 100, 0.25),
        _row(20, 100, 0.22),
        _row(8, 150, 0.21),
        _row(12, 150, 3.00),
        _row(20, 150, 0.23),
        _row(8, 200, 0.19),
        _row(12, 200, 0.24),
        _row(20, 200, 0.20),
    ]
    best, _nb, _note = plateau_check(rows, AXES)
    assert best is not None
    assert best.sharpe < 0.5, "the 3.00 spike must not be the winner"
    assert best.sharpe != 3.00


def test_walks_down_to_the_best_plateau_cell():
    """When the top cell is a spike, the next-best cell on a plateau wins."""
    rows = [
        _row(8, 100, 0.90),
        _row(12, 100, 0.95),
        _row(20, 100, 0.90),
        _row(8, 150, 0.92),
        _row(12, 150, 1.00),
        _row(20, 150, 0.94),
        _row(8, 200, 0.10),
        _row(12, 200, 2.50),
        _row(20, 200, 0.10),
    ]
    best, _nb, _note = plateau_check(rows, AXES)
    assert best is not None
    assert best.sharpe == 1.00, "should skip the 2.50 spike and take the 1.00 plateau"


def test_ma_types_are_not_mixed(monkeypatch):
    """F018 regression.

    EMA has a clean plateau at 1.00; SMA has junk at the same coordinates. If
    the neighbour index collapses on (fast, slow), the EMA candidate gets SMA
    neighbours and the plateau verdict becomes meaningless.
    """
    ema = [
        _row(8, 100, 0.90, "EMA"),
        _row(12, 100, 0.95, "EMA"),
        _row(20, 100, 0.90, "EMA"),
        _row(8, 150, 0.92, "EMA"),
        _row(12, 150, 1.00, "EMA"),
        _row(20, 150, 0.94, "EMA"),
        _row(8, 200, 0.91, "EMA"),
        _row(12, 200, 0.96, "EMA"),
        _row(20, 200, 0.89, "EMA"),
    ]
    sma = [
        _row(8, 100, 0.01, "SMA"),
        _row(12, 100, 0.02, "SMA"),
        _row(20, 100, 0.01, "SMA"),
        _row(8, 150, 0.01, "SMA"),
        _row(12, 150, 0.02, "SMA"),
        _row(20, 150, 0.01, "SMA"),
        _row(8, 200, 0.01, "SMA"),
        _row(12, 200, 0.02, "SMA"),
        _row(20, 200, 0.01, "SMA"),
    ]
    best, nb, _note = plateau_check(ema + sma, AXES)
    assert best is not None, "the EMA plateau must survive the presence of SMA rows"
    assert best.params["ma_type"] == "EMA"
    assert best.sharpe == 1.00
    assert all(n.params["ma_type"] == "EMA" for n in nb), "neighbours crossed MA types"


def test_negative_sharpe_never_accepted():
    rows = [_row(8, 100, -0.5), _row(12, 100, -0.4), _row(20, 100, -0.6)]
    best, _, note = plateau_check(rows, AXES)
    assert best is None
    assert "<= 0" in note


def test_retain_threshold_is_respected():
    """Neighbour at exactly 80% of the peak passes; at 79% the peak is rejected.

    On rejection the walk moves to the shoulder cell, which is itself on a
    plateau (its neighbour is higher). That is the intended semantic: return the
    best cell that is flat, not nothing at all.
    """
    ok = [_row(8, 100, 0.80), _row(12, 100, 1.00), _row(20, 100, 0.80)]
    best_ok, _, _ = plateau_check(ok, ("fast_period",), retain=0.80)
    assert best_ok is not None and best_ok.sharpe == 1.00

    bad = [_row(8, 100, 0.79), _row(12, 100, 1.00), _row(20, 100, 0.79)]
    best_bad, _, _ = plateau_check(bad, ("fast_period",), retain=0.80)
    assert best_bad is not None
    assert best_bad.sharpe == 0.79, "the 1.00 peak is rejected; its shoulder wins"


def test_plateau_check_applies_no_absolute_threshold():
    """Separation of concerns: plateau_check tests SHAPE, the gate tests LEVEL.

    A flat surface of poor Sharpes returns its best cell. Filtering that out is
    S001 §8's job, and conflating the two would hide why a candidate failed.
    """
    rows = [_row(8, 100, 0.10), _row(12, 100, 0.12), _row(20, 100, 0.11)]
    best, _, _ = plateau_check(rows, ("fast_period",))
    assert best is not None and best.sharpe == 0.12


def test_single_cell_cannot_be_assessed():
    """One cell has no neighbours, so no plateau claim can be made about it."""
    best, _, note = plateau_check([_row(12, 150, 2.0)], AXES)
    assert best is None
    assert "unassessable" in note


def test_edge_cell_uses_only_in_grid_neighbours():
    rows = [_row(8, 100, 1.00), _row(12, 100, 0.90), _row(20, 100, 0.50)]
    best, nb, _ = plateau_check(rows, ("fast_period",))
    assert best is not None
    assert best.params["fast_period"] == 8
    assert len(nb) == 1, "the grid edge has one neighbour, not two"


def test_table_renders_and_sorts_by_sharpe():
    rows = [_row(8, 100, 0.5), _row(12, 150, 1.5), _row(20, 200, 1.0)]
    out = table(rows, top=2).splitlines()
    assert len(out) == 4  # header + rule + 2 rows
    assert "1.50" in out[2]
    assert "1.00" in out[3]


def test_sweep_row_sharpe_defaults_to_zero():
    r = SweepRow(params={}, metrics={}, fee_share_of_gross_pct=0.0)
    assert r.sharpe == 0.0


# ---------------- fee share (mutation survivors on lines 45-46) ------------- #
def test_fee_share_of_gross_is_computed_correctly():
    """Mutation testing found `+` -> `-` and `/` -> `*` both survived here.

    `fee/gross` drove the whole 1h-vs-4h turnover conclusion, so an arithmetic
    error here would have mis-stated the central finding of Phase 2.
    """
    from dataclasses import replace

    from ntx.backtest.metrics import Metrics
    from ntx.backtest.sweep import _fee_share

    base = Metrics(
        trades=10,
        net_pnl=0.0,
        return_pct=0.0,
        realized_return_pct=0.0,
        unrealized_at_end_pct=0.0,
        win_rate=0.5,
        profit_factor=1.0,
        avg_win=0.0,
        avg_loss=0.0,
        payoff_ratio=0.0,
        largest_win=0.0,
        largest_loss=0.0,
        max_drawdown_pct=0.0,
        sharpe=0.0,
        sortino=0.0,
        exposure_pct=0.0,
        fees_paid=0.0,
    )

    # net 90 + fees 10 -> gross 100, so fees are 10% of gross
    assert _fee_share(replace(base, net_pnl=90.0, fees_paid=10.0)) == pytest.approx(10.0)
    # net 0 + fees 50 -> gross 50, fees are 100% of gross: the signal ate it all
    assert _fee_share(replace(base, net_pnl=0.0, fees_paid=50.0)) == pytest.approx(100.0)
    # a LOSS with heavy fees: net -50, fees 100 -> gross 50 -> 200%
    assert _fee_share(replace(base, net_pnl=-50.0, fees_paid=100.0)) == pytest.approx(200.0)
    # gross exactly zero must not divide by zero
    assert math.isnan(_fee_share(replace(base, net_pnl=-10.0, fees_paid=10.0)))


def test_rejection_note_states_the_correct_neighbour_ratio():
    """Mutation survivor: `worst / cand.sharpe` -> `worst * cand.sharpe`.

    The note is what a human reads when judging whether a rejection was
    reasonable, so a wrong percentage there is actively misleading.

    A monotonically declining surface ending below zero is used because it is
    the shape that actually yields no winner -- see
    `test_grid_edge_minimum_always_passes` for why.
    """
    rows = [_row(8, 100, 1.00), _row(12, 100, 0.10), _row(20, 100, -0.05)]
    best, _nb, note = plateau_check(rows, ("fast_period",), retain=0.80)
    assert best is None
    # worst neighbour 0.10 / candidate 1.00 = 10%
    assert "10%" in note, f"expected the 10% ratio in: {note}"
    assert "1.00" in note


def test_grid_edge_minimum_always_passes():
    """A documented property of the rule, found while writing the test above.

    A cell passes when `min(neighbours) >= retain * cell`. A LOW cell surrounded
    by higher ones therefore always passes, so `plateau_check` returns nothing
    only when every candidate is non-positive or unassessable. That is why the
    go-live gate applies the absolute threshold separately (S001 §8) -- the
    plateau rule cannot be relied on to reject a weak surface, only a spiky one.
    """
    declining = [_row(8, 100, 1.00), _row(12, 100, 0.50), _row(20, 100, 0.20)]
    best, _nb, _note = plateau_check(declining, ("fast_period",), retain=0.80)
    assert best is not None
    assert best.sharpe == 0.20, "the edge minimum passes: its only neighbour is higher"
