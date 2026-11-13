"""S001 -- MA Cross Trend.

Signal logic ONLY. Sizing, exits, protective stops, risk gates and journalling
all live in `NtxStrategy` (docs/14-extensibility.md §1).

Spec: docs/strategies/S001-ma-cross-trend.md
"""

from __future__ import annotations

from nautilus_trader.indicators.averages import (
    ExponentialMovingAverage,
    SimpleMovingAverage,
)
from nautilus_trader.indicators.trend import DirectionalMovement
from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import InstrumentId

from ntx.strategies.base import NtxStrategy, NtxStrategyConfig
from ntx.strategies.signal import Direction, Signal


class MACrossTrendConfig(NtxStrategyConfig, frozen=True):
    fast_period: int = 20
    slow_period: int = 50
    ma_type: str = "EMA"  # EMA | SMA
    flip_on_cross: bool = True

    # --- optional filters, DEFAULT OFF ---------------------------------------
    # A filter ships only if it improves out-of-sample Sharpe AND drawdown
    # (S001 §2). Better in-sample only means it is fitting noise, and each one
    # adds a knob to overfit with.
    dx_min: float | None = None  # trend-strength gate, 0..100
    dx_period: int = 14
    regime_period: int | None = None  # only long above this MA, short below


def _make_ma(ma_type: str, period: int):
    if ma_type.upper() == "SMA":
        return SimpleMovingAverage(period)
    if ma_type.upper() == "EMA":
        return ExponentialMovingAverage(period)
    raise ValueError(f"unsupported ma_type {ma_type!r}; use EMA or SMA")


class MACrossTrendStrategy(NtxStrategy):
    """Fast MA crossing slow MA, long and short.

    The edge is not predictive accuracy -- expected win rate is 35-45%. It is
    asymmetric payoff: cut losers at a fixed risk unit, let winners run via the
    trailing stop. Anything that caps the upside is suspect even when it raises
    the win rate (S001 §1).
    """

    def __init__(self, config: MACrossTrendConfig) -> None:
        if config.fast_period >= config.slow_period:
            raise ValueError(
                f"fast_period ({config.fast_period}) must be < slow_period ({config.slow_period})"
            )
        if config.dx_min is not None and not 0.0 <= config.dx_min <= 100.0:
            raise ValueError(f"dx_min must be in [0, 100], got {config.dx_min}")
        super().__init__(config)
        self.fast: dict[InstrumentId, object] = {}
        self.slow: dict[InstrumentId, object] = {}
        self.dm: dict[InstrumentId, DirectionalMovement] = {}
        self.regime: dict[InstrumentId, object] = {}
        self._prev_diff: dict[InstrumentId, float | None] = {}
        self.filtered_out = 0

    def warmup_bars(self) -> int:
        # +1 so a cross can be detected on the first fully-warmed bar.
        periods = [self.config.slow_period, self.config.atr_period]
        if self.config.dx_min is not None:
            periods.append(self.config.dx_period)
        if self.config.regime_period is not None:
            periods.append(self.config.regime_period)
        return max(periods) + 1

    def on_indicators_init(self, instrument_id: InstrumentId) -> None:
        self.fast[instrument_id] = _make_ma(self.config.ma_type, self.config.fast_period)
        self.slow[instrument_id] = _make_ma(self.config.ma_type, self.config.slow_period)
        self._prev_diff[instrument_id] = None
        if self.config.dx_min is not None:
            self.dm[instrument_id] = DirectionalMovement(self.config.dx_period)
        if self.config.regime_period is not None:
            self.regime[instrument_id] = _make_ma(self.config.ma_type, self.config.regime_period)

    def on_indicators_update(self, bar: Bar) -> None:
        inst_id = bar.bar_type.instrument_id
        self.fast[inst_id].handle_bar(bar)
        self.slow[inst_id].handle_bar(bar)
        if inst_id in self.dm:
            self.dm[inst_id].handle_bar(bar)
        if inst_id in self.regime:
            self.regime[inst_id].handle_bar(bar)

    def trend_strength(self, inst_id: InstrumentId) -> float | None:
        """DX: |+DI - -DI| / (+DI + -DI) * 100.

        This is the raw DX component of ADX rather than ADX itself (which is a
        smoothed DX); nautilus exposes +DI/-DI but no ADX. DX is noisier but
        measures the same thing -- directional conviction versus chop.
        """
        dm = self.dm.get(inst_id)
        if dm is None or not dm.initialized:
            return None
        total = float(dm.pos) + float(dm.neg)
        if total <= 0:
            return None
        return abs(float(dm.pos) - float(dm.neg)) / total * 100.0

    def _passes_filters(self, inst_id: InstrumentId, direction: Direction, close: float) -> bool:
        if self.config.dx_min is not None:
            dx = self.trend_strength(inst_id)
            if dx is None or dx < self.config.dx_min:
                return False
        regime = self.regime.get(inst_id)
        if regime is not None:
            if not regime.initialized:
                return False
            level = float(regime.value)
            if direction is Direction.LONG and close <= level:
                return False
            if direction is Direction.SHORT and close >= level:
                return False
        return True

    def on_bar_signal(self, bar: Bar) -> Signal | None:
        inst_id = bar.bar_type.instrument_id
        fast, slow = self.fast[inst_id], self.slow[inst_id]
        if not (fast.initialized and slow.initialized):
            return None

        diff = float(fast.value) - float(slow.value)
        prev = self._prev_diff[inst_id]
        self._prev_diff[inst_id] = diff

        if prev is None:
            return None  # need two points to see a cross

        crossed_up = prev <= 0 < diff
        crossed_down = prev >= 0 > diff
        if not (crossed_up or crossed_down):
            return None

        direction = Direction.LONG if crossed_up else Direction.SHORT
        if not self._passes_filters(inst_id, direction, float(bar.close)):
            self.filtered_out += 1
            return None

        tag = "cross_up" if crossed_up else "cross_down"
        return Signal(
            str(inst_id),
            direction,
            reason=f"{tag} fast={fast.value:.6g} slow={slow.value:.6g}",
        )
