"""S003 -- Donchian Breakout.

Buy when price makes a new N-bar high; sell when it makes a new N-bar low. This
is the Turtle Traders rule from the 1980s, the most-documented mechanical trend
system there is, and it reuses S001's entire framework -- only the entry rule
differs (docs/14-extensibility.md §1).

**Why it is being tested:** S001 failed its out-of-sample gate with the win rate
intact and the payoff collapsed. That leaves an open question this strategy is
designed to answer: is trend-following itself not working on crypto perps in
this period, or is an MA cross specifically a weak way to detect a trend?
S003 is the same family with a different detector. Both failing points at the
regime; S003 working points at the detector.

Spec: docs/strategies/S003-donchian-breakout.md
"""

from __future__ import annotations

from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import InstrumentId

from ntx.strategies.base import NtxStrategy, NtxStrategyConfig
from ntx.strategies.signal import Direction, Signal


class DonchianBreakoutConfig(NtxStrategyConfig, frozen=True):
    entry_period: int = 20  # bars of high/low that define a breakout
    exit_period: int = 10  # opposite break that closes a position
    use_exit_break: bool = True  # else rely purely on the SL/TSL stack


class DonchianBreakoutStrategy(NtxStrategy):
    """Breakout of the prior `entry_period` extreme.

    The channel is taken from the **prior** bars, never including the current
    one. A Donchian channel that includes the live bar makes
    `close > channel_high` almost tautological, since the close contributed to
    that high -- the classic way this rule gets accidentally neutered.
    """

    def __init__(self, config: DonchianBreakoutConfig) -> None:
        if config.entry_period < 2:
            raise ValueError(f"entry_period must be >= 2, got {config.entry_period}")
        if config.exit_period < 1:
            raise ValueError(f"exit_period must be >= 1, got {config.exit_period}")
        if config.exit_period >= config.entry_period:
            raise ValueError(
                f"exit_period ({config.exit_period}) must be < entry_period "
                f"({config.entry_period}); a wider exit than entry never closes"
            )
        super().__init__(config)
        # Rolling highs/lows of COMPLETED prior bars, per instrument.
        self._highs: dict[InstrumentId, list[float]] = {}
        self._lows: dict[InstrumentId, list[float]] = {}

    def warmup_bars(self) -> int:
        return max(self.config.entry_period, self.config.atr_period) + 1

    def on_indicators_init(self, instrument_id: InstrumentId) -> None:
        self._highs[instrument_id] = []
        self._lows[instrument_id] = []

    def on_indicators_update(self, bar: Bar) -> None:
        """Record every bar, history included.

        The channel used to be appended to inside `on_bar_signal`, which only
        runs once `bars_seen >= warmup_bars()`. After a restart that left the
        55-bar channel empty for another 49 live bars -- 8.2 days with no entry
        possible, while the monitor reported `warm: true` (R-01, review
        2026-09-18). `on_indicators_update` runs for warm-up bars too, so the
        channel is usable the moment the history request lands.
        """
        inst_id = bar.bar_type.instrument_id
        if inst_id not in self._highs:
            self.on_indicators_init(inst_id)
        self._highs[inst_id].append(float(bar.high))
        self._lows[inst_id].append(float(bar.low))
        keep = max(self.config.entry_period, self.config.exit_period) + 2
        if len(self._highs[inst_id]) > keep:
            self._highs[inst_id] = self._highs[inst_id][-keep:]
            self._lows[inst_id] = self._lows[inst_id][-keep:]

    def channel(self, inst_id: InstrumentId, period: int) -> tuple[float, float] | None:
        """(highest high, lowest low) over the last `period` PRIOR bars.

        The bar being judged is already recorded (it is appended in
        `on_indicators_update`, which runs first), so it is excluded here --
        otherwise `close > channel_high` would be near-tautological.
        """
        highs, lows = self._highs.get(inst_id, []), self._lows.get(inst_id, [])
        if len(highs) < period + 1:
            return None
        return max(highs[-period - 1 : -1]), min(lows[-period - 1 : -1])

    def signal_ready(self, inst_id: InstrumentId) -> bool:
        """A signal is possible: enough prior bars for the entry channel."""
        return self.channel(inst_id, self.config.entry_period) is not None

    def on_bar_signal(self, bar: Bar) -> Signal | None:
        inst_id = bar.bar_type.instrument_id
        close = float(bar.close)
        entry = self.channel(inst_id, self.config.entry_period)
        exit_ch = (
            self.channel(inst_id, self.config.exit_period) if self.config.use_exit_break else None
        )

        if entry is None:
            return None
        hi, lo = entry

        if close > hi:
            return Signal(
                str(inst_id), Direction.LONG, reason=f"break_up close={close:.6g} hi={hi:.6g}"
            )
        if close < lo:
            return Signal(
                str(inst_id), Direction.SHORT, reason=f"break_down close={close:.6g} lo={lo:.6g}"
            )

        # An opposite break of the shorter channel closes an open position.
        # Expressed as a FLAT signal; the framework's exit stack sees it as the
        # opposite of whatever is held and closes.
        if exit_ch is not None:
            ex_hi, ex_lo = exit_ch
            state = self.states.get(inst_id)
            if state is not None:
                if state.direction is Direction.LONG and close < ex_lo:
                    return Signal(
                        str(inst_id),
                        Direction.SHORT,
                        reason=f"exit_break close={close:.6g} lo={ex_lo:.6g}",
                    )
                if state.direction is Direction.SHORT and close > ex_hi:
                    return Signal(
                        str(inst_id),
                        Direction.LONG,
                        reason=f"exit_break close={close:.6g} hi={ex_hi:.6g}",
                    )
        return None
