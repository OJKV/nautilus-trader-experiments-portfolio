"""Strategy registry.

Adding a strategy is one class, one spec doc and one line here -- no branching in
the node, the risk layer or the backtest runner (docs/14-extensibility.md §3).
"""

from __future__ import annotations

from ntx.strategies.base import NtxStrategy
from ntx.strategies.cross_sectional_momentum import (
    CrossSectionalMomentumConfig,
    CrossSectionalMomentumStrategy,
)
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.ma_cross_trend import MACrossTrendConfig, MACrossTrendStrategy
from ntx.strategies.rehearsal import RehearsalConfig, RehearsalStrategy

STRATEGIES: dict[str, tuple[type[NtxStrategy], type]] = {
    # S001 is ARCHIVED -- it failed the out-of-sample gate (S001 §9e). Kept
    # registered so its results stay reproducible, not for deployment.
    "S001": (MACrossTrendStrategy, MACrossTrendConfig),
    "S003": (DonchianBreakoutStrategy, DonchianBreakoutConfig),
    # S004 FAILED its out-of-sample gate and is DEMO ONLY (see DEMO_ONLY below).
    "S004": (CrossSectionalMomentumStrategy, CrossSectionalMomentumConfig),
    # Plumbing rehearsal: enters unconditionally. Never on real money.
    "S900": (RehearsalStrategy, RehearsalConfig),
}

# Parameters of the VALIDATED configuration, one source of truth for the gate
# experiment and the live node (F043: the node once ran class defaults 20/10).
# Every exit parameter is pinned so a change of class defaults cannot drift in.
DEPLOYED: dict[str, dict] = {
    # DEMO ONLY -- failed OOS. Pinned so the deployed run stays reproducible
    # (R47), exactly as for a strategy that passed.
    "S004": dict(
        lookback=90,
        rebalance_bars=6,
        long_n=3,
        short_n=0,
        min_ranked=5,
        reversal=False,
        # NOT bar_spec: node assembly supplies it from the plan, and pinning it
        # here made the node die with "dict() got multiple values for keyword
        # argument 'bar_spec'" after every start-up assertion had passed. The
        # validated runs used 4h, which is also the node's default.
        sl_atr_mult=2.0,
        tsl_enabled=True,
        tsl_activation_price_pct=1.5,
        tsl_trail_price_pct=1.0,
        risk_equity_pct=0.5,
        max_leverage=3.0,
        max_concurrent_positions=3,
    ),
    "S003": dict(
        entry_period=55,
        exit_period=20,
        sl_atr_mult=2.0,
        tsl_enabled=True,
        tsl_activation_price_pct=1.5,
        tsl_trail_price_pct=1.0,
        # Risk parameters every recorded gate run used. Pinned here so that
        # .env cannot silently run something else: on 2026-09-21 the server's
        # .env said 5 positions and five orders went out at once (R47).
        risk_equity_pct=0.5,
        max_leverage=3.0,
        max_concurrent_positions=3,
    ),
}

# Strategies `live.node.plan` refuses outside the demo environment.
#
# REHEARSAL_ONLY additionally means "flatten everything on stop" -- S900's
# semantics, because a rehearsal must never leave a position behind. DEMO_ONLY
# means only "never on real money"; a strategy in it keeps its positions across a
# restart like any other, which is the whole point when it exists to generate
# execution history.
REHEARSAL_ONLY: frozenset[str] = frozenset({"S900"})

# S004 cross-sectional momentum FAILED the out-of-sample gate on 2026-09-25:
# profit factor after funding 1.07 against 1.15 required, fee drag 64% of gross,
# breadth 4/10 instruments positive against the 6 docs/09 requires, and the top
# ten trades were 326% of the total return -- the rest of the book loses money.
# Its in-sample winner is a single cell whose four neighbours all fail PF and all
# latch the drawdown KILL (F024's isolated-peak shape).
#
# It is deployed to a demo sub-account anyway, and only for this: it trades about
# 1.36 times a day against S003's 0.83, so entries, venue stops, fills, adoption
# and the watchdog are exercised far more often. Every real defect found in the
# week before go-live came from the execution path, not from a signal.
#
# Promotion would require re-running the whole gate, not deleting this line.
DEMO_ONLY: frozenset[str] = frozenset({"S004"})


def get(strategy_id: str) -> tuple[type[NtxStrategy], type]:
    try:
        return STRATEGIES[strategy_id]
    except KeyError:
        raise KeyError(f"unknown strategy {strategy_id!r}; known: {sorted(STRATEGIES)}") from None
