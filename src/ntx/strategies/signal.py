"""Signal and exit vocabulary shared by every strategy.

A `Signal` expresses **intent only**. It cannot set size, leverage, or widen a
stop -- those are framework decisions (docs/14-extensibility.md §2). This is the
single invariant that stops a badly-behaved or model-driven strategy from
becoming a risk event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"

    @property
    def sign(self) -> int:
        return {Direction.LONG: 1, Direction.SHORT: -1, Direction.FLAT: 0}[self]

    @property
    def opposite(self) -> Direction:
        return {
            Direction.LONG: Direction.SHORT,
            Direction.SHORT: Direction.LONG,
            Direction.FLAT: Direction.FLAT,
        }[self]


class ExitReason(StrEnum):
    """Why a position closed. Recorded per trade for PnL attribution -- knowing
    which exit earns the money is how the exit stack gets tuned (docs/09 §6)."""

    OPPOSITE_SIGNAL = "opposite_signal"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    TAKE_PROFIT = "take_profit"
    ROI_TARGET = "roi_target"
    TIME_STOP = "time_stop"
    RISK_HALT = "risk_halt"
    STOP_REFUSED = "stop_refused"
    KILL_SWITCH = "kill_switch"


@dataclass(frozen=True)
class Signal:
    instrument_id: str
    direction: Direction
    strength: float = 1.0  # 0..1 conviction; framework may ignore it
    reason: str = ""  # logged for attribution
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"strength must be in [0, 1], got {self.strength}")
