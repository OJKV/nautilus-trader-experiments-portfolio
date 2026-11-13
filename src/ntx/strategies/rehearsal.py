"""S900 -- plumbing rehearsal. DEMO ONLY (refused on live by `live.node.plan`).

Not a strategy: it enters once per instrument on the first warm bar and exits
after `hold_bars` bars, so a short demo session exercises every live code path
that a 4h strategy would take days to reach -- market entry, fill-then-attach
stop (F007), protect-gap measurement, journal writes, strategy exit, stop
cancellation, and flatten-on-stop.
"""

from __future__ import annotations

from dataclasses import replace

from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import InstrumentId

from ntx.strategies.base import NtxStrategy, NtxStrategyConfig
from ntx.strategies.signal import Direction, Signal


class RehearsalConfig(NtxStrategyConfig, frozen=True):
    hold_bars: int = 3
    side: str = "LONG"  # LONG | SHORT
    tsl_enabled: bool = False
    # Stop well away from price: the rehearsal tests the stop's EXISTENCE,
    # not its triggering, and 1-minute ATR is tiny.
    sl_atr_mult: float | None = 10.0


class RehearsalStrategy(NtxStrategy):
    def __init__(self, config: RehearsalConfig) -> None:
        if config.side not in ("LONG", "SHORT"):
            raise ValueError(f"side must be LONG or SHORT, got {config.side!r}")
        super().__init__(config)
        # Exit through the framework's time stop, not an opposite signal --
        # an opposite signal would flip straight into a new position.
        self.exit_cfg = replace(self.exit_cfg, max_bars_in_position=config.hold_bars)
        self._done: set[InstrumentId] = set()

    def warmup_bars(self) -> int:
        return self.config.atr_period + 1

    def _enter(self, inst_id, direction, bar) -> None:
        # Only reached from live bars, so the one-shot is consumed here.
        self._done.add(inst_id)
        super()._enter(inst_id, direction, bar)

    def on_bar_signal(self, bar: Bar) -> Signal | None:
        inst_id = bar.bar_type.instrument_id
        # Do NOT mark done here: this also runs on warm-up history (F033).
        if inst_id in self._done or inst_id in self.states:
            return None
        return Signal(str(inst_id), Direction[self.config.side], reason="rehearsal entry")
