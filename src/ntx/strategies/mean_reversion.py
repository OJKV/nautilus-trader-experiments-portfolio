"""S008 -- Mean Reversion (rolling z-score). Signal logic ONLY; the rest is NtxStrategy.

**Why this family, and why now.** S003 is a breakout: it makes money when a range
*breaks*. Every trade it takes is a bet that the current move continues. That is
one bet, repeated. A second strategy that is merely a faster breakout adds
turnover without adding information -- and the scout's measurements say the
Donchian signal's per-trade expectancy degrades roughly in step with the rate,
so a faster S003 is strictly worse. Mean reversion is the opposite bet: it makes
money when a range *holds*. If it works at all, its losing periods should be
S003's winning periods, which is most of its value (measured in
`experiments/mean_reversion_study.py --correlate`).

**The rule.** For each bar, take the closes of the `lookback` bars that closed
*before* it, and compute their mean and standard deviation. The z-score is how
many of those standard deviations the current close sits away from that mean.
A close far *below* the recent mean is a long; far *above* is a short.

**Why the current bar is excluded from its own window.** Including it makes the
measurement partly self-referential: an outlier close drags the mean towards
itself and inflates the standard deviation it is then divided by, which shrinks
the very z-score it should be producing. With `lookback=5` a close at exactly
-2 sigma of the prior window measures -1.41 sigma if it is included in its own
window -- it would not fire at a threshold of 2.0. This is the same class of
error as a Donchian channel that includes the live bar
(`donchian_breakout.py`), and `test_mean_reversion.py::test_window_excludes_the
_current_bar` pins the arithmetic.

**The sigma floor.** Dividing by a near-zero standard deviation manufactures
enormous z-scores out of nothing: five identical closes followed by a 0.1% tick
is not a two-sigma dislocation, it is a quiet market. `min_sigma_price_pct` is a
hard floor on the rolling sigma as a fraction of price; below it no signal is
produced, and every signal so suppressed is counted in `filtered_out` so the
guard can never be a silent no-op (R40).

**What this file does NOT do -- exits.** There is no "close at the mean" rule
here, and that is a framework constraint rather than a design choice. A
`Direction.FLAT` signal is inert: `evaluate_exit` (`exits.py:181`) only closes on
a signal that is the *opposite* of the open position, and the base class then
flips into it (`base.py:880`). So the only way to express "revert to the mean and
go flat" would be to emit the opposite direction, which would open a fresh
position at z = 0 -- precisely where this strategy has no edge. Exits therefore
come from the inherited stack: the mandatory ATR stop, the trailing stop, and
the time stop (`max_bars_in_position`, set in the deployed config, not here).
An *overshoot* to the opposite band is a genuine opposite signal and does flip,
which is the same stop-and-reverse S003 uses.

**The known hazard.** Mean reversion in crypto buys falling knives: the left
tail is fat and a dislocation is often the start of a trend, not a rubber band.
The inherited ATR stop is what stands between this rule and a 2020-03-12. It
matters more here than in any trend strategy, which is why `sl_atr_mult` is
never widened to "give the trade room".
"""

from __future__ import annotations

from statistics import fmean, pstdev

from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import InstrumentId

from ntx.strategies.base import NtxStrategy, NtxStrategyConfig
from ntx.strategies.signal import Direction, Signal


class MeanReversionConfig(NtxStrategyConfig, frozen=True):
    lookback: int = 96  # prior bars defining the mean and sigma
    entry_z: float = 2.0  # |z| at or beyond which a dislocation is tradable
    # The rolling sigma must be at least this share of price for the z-score to
    # carry information. R4: a move in the asset price, hence `_price_pct`.
    min_sigma_price_pct: float = 0.05


class MeanReversionStrategy(NtxStrategy):
    """Fade a close that is `entry_z` rolling standard deviations from the mean."""

    def __init__(self, config: MeanReversionConfig) -> None:
        # Validate before super(): a bad config should fail at construction, not
        # halfway through a four-year backtest.
        if config.lookback < 2:
            raise ValueError(f"lookback must be >= 2, got {config.lookback}")
        if config.entry_z <= 0:
            raise ValueError(f"entry_z must be > 0, got {config.entry_z}")
        if config.min_sigma_price_pct < 0:
            raise ValueError(f"min_sigma_price_pct must be >= 0, got {config.min_sigma_price_pct}")
        super().__init__(config)
        # Rolling closes of COMPLETED bars, per instrument.
        self._closes: dict[InstrumentId, list[float]] = {}
        # Signals suppressed by the sigma floor. Read by the backtest runner
        # (`runner.py:203`) into `counters['filtered_out']` so that a guard which
        # never fires is distinguishable from a guard that fires constantly (R40).
        self.filtered_out = 0

    def warmup_bars(self) -> int:
        # Must cover both the z-score window and the framework's ATR, plus the
        # one extra bar the window excludes.
        return max(self.config.lookback, self.config.atr_period) + 1

    def on_indicators_init(self, instrument_id: InstrumentId) -> None:
        self._closes[instrument_id] = []

    def on_indicators_update(self, bar: Bar) -> None:
        """Record every bar, warm-up history included.

        R46/F033: this is the only correct place for rolling state.
        `on_bar_signal` does not run until `bars_seen >= warmup_bars()`, so a
        buffer filled there would still be empty for `lookback` more *live* bars
        after every restart while the monitor cheerfully reported `warm: true`
        (R-01, the 8.2-day dead zone S003 shipped with).
        """
        inst_id = bar.bar_type.instrument_id
        if inst_id not in self._closes:
            self.on_indicators_init(inst_id)
        buf = self._closes[inst_id]
        buf.append(float(bar.close))
        keep = self.config.lookback + 2
        if len(buf) > keep:
            del buf[:-keep]

    def stats(self, inst_id: InstrumentId) -> tuple[float, float] | None:
        """(mean, population sigma) of the `lookback` bars PRIOR to the current one.

        The bar being judged has already been appended by `on_indicators_update`,
        which runs first, so it is sliced out here.
        """
        buf = self._closes.get(inst_id, [])
        n = self.config.lookback
        if len(buf) < n + 1:
            return None
        window = buf[-n - 1 : -1]
        return fmean(window), pstdev(window)

    def zscore(self, inst_id: InstrumentId, close: float) -> float | None:
        """Signed dislocation in prior-window sigmas, or None if not measurable."""
        st = self.stats(inst_id)
        if st is None:
            return None
        mean, sigma = st
        if sigma <= 0.0:
            return None
        return (close - mean) / sigma

    def signal_ready(self, instrument_id: InstrumentId) -> bool:
        """R52/R-01: 'a signal is possible', not 'I have counted N bars'."""
        return self.stats(instrument_id) is not None

    def on_bar_signal(self, bar: Bar) -> Signal | None:
        inst_id = bar.bar_type.instrument_id
        close = float(bar.close)
        st = self.stats(inst_id)
        if st is None:
            return None
        mean, sigma = st
        # A flat window has no scale, so there is no such thing as a dislocation
        # in it. Checked independently of the floor below, which can be
        # configured to 0 and then would not catch this
        # (`test_zero_sigma_does_not_divide_by_zero`).
        if sigma <= 0.0:
            return None

        # Quiet-market guard. A sigma this small is measurement noise, and the
        # z-score it produces is an artefact of the divisor, not a dislocation.
        floor = close * self.config.min_sigma_price_pct / 100.0
        if sigma < floor:
            # Count only the signals this actually suppressed, so the number
            # means "vetoed entries" and not "bars the branch was reached on".
            if abs(close - mean) / sigma >= self.config.entry_z:
                self.filtered_out += 1
            return None

        z = (close - mean) / sigma
        if z <= -self.config.entry_z:
            return Signal(
                str(inst_id),
                Direction.LONG,
                reason=f"z={z:.2f} close={close:.6g} mean={mean:.6g} sigma={sigma:.6g}",
            )
        if z >= self.config.entry_z:
            return Signal(
                str(inst_id),
                Direction.SHORT,
                reason=f"z={z:.2f} close={close:.6g} mean={mean:.6g} sigma={sigma:.6g}",
            )
        return None
