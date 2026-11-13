"""Random-entry control (docs/09-backtest-methodology.md §6, benchmark 3).

The most important benchmark in the methodology, and the cheapest to skip.

It enters at random with the **same exit stack, same sizing and same risk layer**
as the strategy under test, at a matched trade frequency. If the real strategy
cannot beat this, then the exits are the edge and the signal contributes
nothing -- which is worth knowing before deploying a signal.

Not a strategy. Never registered for live trading.
"""

from __future__ import annotations

import random

from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import InstrumentId

from ntx.strategies.base import NtxStrategy, NtxStrategyConfig
from ntx.strategies.signal import Direction, Signal


class RandomEntryConfig(NtxStrategyConfig, frozen=True):
    entry_probability: float = 0.02  # per bar, per instrument
    seed: int = 42  # reproducible (R9)
    warmup: int = 60


class RandomEntryStrategy(NtxStrategy):
    """Coin-flip entries, identical machinery everywhere else."""

    def __init__(self, config: RandomEntryConfig) -> None:
        if not 0.0 < config.entry_probability <= 1.0:
            raise ValueError("entry_probability must be in (0, 1]")
        super().__init__(config)
        self._rng = random.Random(config.seed)

    def warmup_bars(self) -> int:
        return max(self.config.warmup, self.config.atr_period + 1)

    def on_indicators_init(self, instrument_id: InstrumentId) -> None:
        pass

    def on_bar_signal(self, bar: Bar) -> Signal | None:
        if self._rng.random() >= self.config.entry_probability:
            return None
        direction = Direction.LONG if self._rng.random() < 0.5 else Direction.SHORT
        return Signal(str(bar.bar_type.instrument_id), direction, reason="random")
