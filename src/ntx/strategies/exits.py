"""The exit stack -- pure decision logic, no engine, no venue, no clock.

Kept pure on purpose: these rules decide when money is taken off the table, so
they must be testable exhaustively without spinning up a backtest. The strategy
class wires them to orders; this module only answers "should this position close,
and why".

Evaluation order is fixed and first-match-wins (S001 §4):

    1. opposite signal      (trend-following's native exit)
    2. stop loss            (hard risk limit)
    3. trailing stop        (protects open profit)
    4. take profit          (fixed price move; default off)
    5. ROI target           (return on margin; default off)
    6. time stop            (prevents zombie positions; default off)

Stop loss is checked before trailing so that a bar which breaches both is
attributed to the stop -- the pessimistic reading, and the one that matches a
venue evaluating both orders on the same tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ntx.strategies.signal import Direction, ExitReason

__all__ = ["ExitConfig", "PositionState", "TrailingStop", "evaluate_exit"]


@dataclass(frozen=True)
class ExitConfig:
    """Exit parameters. Percentages carry their denominator (R4).

    `*_price_pct` = move in the asset price.
    `*_roi_pct`   = return on the margin allocated to this position.
    """

    # stop loss: ATR-based by default, fixed-% as an alternative for the sweep
    sl_atr_mult: float | None = 2.0
    sl_price_pct: float | None = None

    # trailing stop: arm after a favourable move, then trail
    tsl_enabled: bool = True
    tsl_activation_price_pct: float = 1.5
    tsl_trail_price_pct: float = 1.0

    # off by default -- see ADR-0008
    tp_price_pct: float | None = None
    target_roi_pct: float | None = None
    max_bars_in_position: int | None = None

    def __post_init__(self) -> None:
        if self.sl_atr_mult is None and self.sl_price_pct is None:
            raise ValueError("a stop loss is mandatory: set sl_atr_mult or sl_price_pct")
        if self.sl_atr_mult is not None and self.sl_price_pct is not None:
            raise ValueError("set exactly one of sl_atr_mult / sl_price_pct, not both")
        for name in ("tsl_activation_price_pct", "tsl_trail_price_pct"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")


class TrailingStop:
    """Monotonic trailing stop.

    Arms once price has moved `activation_pct` in favour, then trails
    `trail_pct` behind the best price seen. **Never moves against the
    position** -- asserted by `test_trailing_stop_is_monotonic`. A trailing stop
    that can loosen is not a trailing stop; it is a way to give back profit.
    """

    __slots__ = ("_best", "_level", "activation_pct", "direction", "entry_price", "trail_pct")

    def __init__(
        self, direction: Direction, entry_price: float, activation_pct: float, trail_pct: float
    ) -> None:
        if direction is Direction.FLAT:
            raise ValueError("a trailing stop needs a direction")
        self.direction = direction
        self.entry_price = entry_price
        self.activation_pct = activation_pct
        self.trail_pct = trail_pct
        self._best: float | None = None
        self._level: float | None = None

    @property
    def armed(self) -> bool:
        return self._level is not None

    @property
    def level(self) -> float | None:
        return self._level

    @property
    def best_price(self) -> float | None:
        return self._best

    def favourable_move_pct(self, price: float) -> float:
        """Signed move in favour of the position, as a percentage of entry."""
        move = (price - self.entry_price) / self.entry_price * 100.0
        return move * self.direction.sign

    def update(self, price: float) -> float | None:
        """Feed a price. Returns the (possibly new) stop level, or None if unarmed."""
        if self._best is None or self.favourable_move_pct(price) > self.favourable_move_pct(
            self._best
        ):
            self._best = price

        if not self.armed and self.favourable_move_pct(price) < self.activation_pct:
            return None

        candidate = (
            self._best * (1 - self.trail_pct / 100.0)
            if self.direction is Direction.LONG
            else self._best * (1 + self.trail_pct / 100.0)
        )
        if self._level is None:
            self._level = candidate
        elif self.direction is Direction.LONG:
            self._level = max(self._level, candidate)  # monotonic up
        else:
            self._level = min(self._level, candidate)  # monotonic down
        return self._level

    def is_breached(self, price: float) -> bool:
        if self._level is None:
            return False
        return price <= self._level if self.direction is Direction.LONG else price >= self._level


@dataclass
class PositionState:
    """Everything the exit rules need about an open position."""

    direction: Direction
    entry_price: float
    stop_price: float
    margin_usd: float
    bars_held: int = 0
    trailing: TrailingStop | None = None
    base_units: float = 0.0
    meta: dict = field(default_factory=dict)

    def price_move_pct(self, price: float) -> float:
        """Favourable move in the asset price, as a percentage of entry."""
        return (price - self.entry_price) / self.entry_price * 100.0 * self.direction.sign

    def unrealised_pnl_usd(self, price: float) -> float:
        return (price - self.entry_price) * self.direction.sign * self.base_units

    def roi_pct(self, price: float) -> float:
        """Return on the margin allocated to this position.

        Explicitly NOT the price move: at 3x leverage a 1% price move is a 3%
        ROI, and conflating the two is the bug R4 exists to prevent.
        """
        if self.margin_usd <= 0:
            return 0.0
        return self.unrealised_pnl_usd(price) / self.margin_usd * 100.0

    def stop_breached(self, price: float) -> bool:
        return (
            price <= self.stop_price
            if self.direction is Direction.LONG
            else price >= self.stop_price
        )


def evaluate_exit(
    state: PositionState,
    price: float,
    cfg: ExitConfig,
    *,
    signal_direction: Direction | None = None,
) -> ExitReason | None:
    """Return the first matching exit reason, or None to stay in the position.

    `price` should be the **bar close** -- exits are decided on the bar close and
    filled at that bar's close, with slippage modelling the gap to a real fill
    (amended R8, F014). Intrabar stop fills are the venue's job
    and are modelled by the backtest's own stop orders, not here.
    """
    if state.direction is Direction.FLAT:
        return None

    # 1. opposite signal -- the native trend-following exit
    if signal_direction is not None and signal_direction is state.direction.opposite:
        return ExitReason.OPPOSITE_SIGNAL

    # 2. hard stop, checked before trailing so a bar breaching both is
    #    attributed to the stop (pessimistic, matches venue behaviour)
    if state.stop_breached(price):
        return ExitReason.STOP_LOSS

    # 3. trailing stop
    if cfg.tsl_enabled and state.trailing is not None:
        state.trailing.update(price)
        if state.trailing.is_breached(price):
            return ExitReason.TRAILING_STOP

    # 4. fixed take profit, in price terms
    if cfg.tp_price_pct is not None and state.price_move_pct(price) >= cfg.tp_price_pct:
        return ExitReason.TAKE_PROFIT

    # 5. ROI target, in margin terms
    if cfg.target_roi_pct is not None and state.roi_pct(price) >= cfg.target_roi_pct:
        return ExitReason.ROI_TARGET

    # 6. time stop
    if cfg.max_bars_in_position is not None and state.bars_held >= cfg.max_bars_in_position:
        return ExitReason.TIME_STOP

    return None


def initial_stop_price(
    direction: Direction, entry_price: float, cfg: ExitConfig, atr: float | None
) -> float:
    """Stop price at entry, from ATR or a fixed percentage.

    ATR is preferred (S001 §4): a fixed 2% stop is far too tight for DOGE and too
    loose for BTC, so a fixed stop silently changes risk per asset.
    """
    if cfg.sl_atr_mult is not None:
        if atr is None or atr <= 0:
            raise ValueError("ATR-based stop requires a positive ATR")
        distance = cfg.sl_atr_mult * atr
    else:
        distance = entry_price * (cfg.sl_price_pct or 0.0) / 100.0
    if distance <= 0:
        raise ValueError("stop distance must be positive")
    return entry_price - distance if direction is Direction.LONG else entry_price + distance
