"""S007 -- Fast Donchian breakout. The same rule as S003, on a shorter bar.

**Why this exists.** S003 (Donchian 55/20 on 4h bars) averages ~0.87 entries per
day across ten instruments, in clusters. The live execution path therefore gets
exercised rarely, and a defect in it can sit undiscovered for days. The owner
wants a second strategy trading 3-5x that rate so the path is rehearsed often,
which must not be bought by deploying something that loses money.

**Why the signal is deliberately IDENTICAL to S003's.** This is the control. If
simply shortening the bar buys the trade rate without destroying the edge, no
cleverer signal is worth its complexity; if it does destroy the edge, that is
the measurement that justifies looking elsewhere. Adding any filter here would
make the comparison meaningless, so this class adds none.

**What actually has to change is the exit stack, not the signal** -- see
`scaled_exits`. That is the whole substance of this module.

Spec: no separate doc yet; this is S003's rule, so docs/strategies/S003-donchian-breakout.md
describes the signal. Only the bar size and the exit scaling differ.
"""

from __future__ import annotations

from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy

# Bar duration in minutes, for the timeframes the catalog actually holds
# (`src/ntx/config/universe.py` TIMEFRAMES). Used only to scale the exit stack.
TF_MINUTES: dict[str, int] = {"15m": 15, "1h": 60, "4h": 240}

# S003's validated exit stack, and the bar it was validated on. Copied rather
# than imported from `registry.DEPLOYED` on purpose: S003's pinned numbers must
# be free to change without silently re-scaling a second strategy's stops.
REFERENCE_TIMEFRAME = "4h"
REFERENCE_EXITS: dict[str, float] = {
    "sl_atr_mult": 2.0,
    "tsl_activation_price_pct": 1.5,
    "tsl_trail_price_pct": 1.0,
}


def scaled_exits(timeframe: str) -> dict[str, float]:
    """S003's exit stack re-expressed for a shorter bar.

    WHY this is not optional. A bar's true range grows roughly with the square
    root of its duration, so `sl_atr_mult=2.0` that buys a ~4% stop on 4h bars
    buys a ~2% stop on 1h bars and a ~1% stop on 15m ones. Under risk-based
    sizing, notional = risk / stop_fraction, so a stop half as wide doubles the
    notional traded and therefore doubles fee drag *at an unchanged trade
    count*. A 1.0% trailing stop is likewise a fixed distance in price, but it
    is evaluated 4x (1h) or 16x (15m) more often, so it is taken out by noise
    long before the signal has been proven wrong.

    Left unscaled, a "faster Donchian" measures the exit stack rather than the
    breakout rule. The scaling here is `sqrt(reference_bar / bar)`, applied to
    the stop multiple and both trailing distances, so the stop stays the same
    fraction of price and the trail stays the same multiple of the stop.

    `atr_period` is deliberately NOT scaled: averaging more bars smooths the ATR
    but does not widen it, so lengthening it does not restore stop width. It
    only inflates `warmup_bars()`, and every restart of the live node requests
    `warmup_bars() + 5` bars in one call.
    """
    if timeframe not in TF_MINUTES:
        raise ValueError(f"unknown timeframe {timeframe!r}; known: {sorted(TF_MINUTES)}")
    factor = (TF_MINUTES[REFERENCE_TIMEFRAME] / TF_MINUTES[timeframe]) ** 0.5
    return {k: round(v * factor, 6) for k, v in REFERENCE_EXITS.items()}


class FastDonchianConfig(DonchianBreakoutConfig, frozen=True):
    """S003's knobs under a distinct type.

    A distinct config class is what lets the registry pin a second validated
    configuration (R47) without S003's class defaults drifting into it, and what
    lets the two strategies run as separate nodes on separate sub-accounts.
    """


class FastDonchianStrategy(DonchianBreakoutStrategy):
    """S003's breakout rule, unchanged, on a shorter bar.

    No override of `on_bar_signal`, `channel` or `signal_ready` -- that absence
    is the point. Everything that differs between S003 and S007 lives in the
    configuration, so the two can be compared without a confound.
    """
