"""Volatility Squeeze Breakout. Signal logic ONLY; everything else is NtxStrategy.

**The claim being tested.** Volatility clusters: a quiet period is followed by a
loud one more often than chance allows. So a break of the recent range that
happens *out of a genuine compression* should continue more often than a break
out of ordinary noise. If that is true, filtering breakouts by "was it quiet
before?" buys a better win rate per trade at a given trade rate -- which is
exactly what a faster strategy needs, because the fee wall (docs/09, S001 §9a)
punishes turnover that carries no edge.

**Why it is a candidate for a second live strategy.** S003 Donchian 55/20 on 4h
averages ~0.87 entries/day across the ten instruments, in clusters. The
execution path therefore gets exercised rarely. A 1h squeeze breakout with short
holds fires several times a day *without* simply being "Donchian on a faster
clock" -- the scout measured that a faster Donchian degrades per-trade
expectancy roughly in step with its rate increase. The squeeze filter is the
only part of this design that is supposed to pay for the extra turnover, so it
is swept against `squeeze_percentile=100` (filter off) as an explicit control:
if the filtered and unfiltered cells have the same win rate, the idea is dead
and must be reported dead (R40).

Three deliberate non-decisions, all inherited from `NtxStrategy` (R24):
sizing, the stop, and the exit stack. In particular the position is closed by
`sl_atr_mult` / the trailing stop / `max_bars_in_position`, all base-class
knobs. This module emits intent and nothing else.

Strategy id: NOT assigned here. `S004`-`S006` are taken by stubs in
`docs/strategies/`, and a sibling experiment is already writing run labels
under `s007-`, so the id belongs in `registry.py` -- which this module does
not own -- and the recorded runs for this study carry the labels
`s007-sweep-IS-c##` and `s007-tf4h-*` regardless of what it ends up called.

Causality (R8/F014): every quantity this rule reads is from a bar that has
CLOSED. The breakout range and the compression reading both exclude the bar
being judged; only the bar's own close and true range -- which are known at its
close -- are used for the break and the expansion test.
"""

from __future__ import annotations

import math

from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import InstrumentId

from ntx.strategies.base import NtxStrategy, NtxStrategyConfig
from ntx.strategies.signal import Direction, Signal


class VolatilityBreakoutConfig(NtxStrategyConfig, frozen=True):
    """Knobs of the signal only. Exit/risk knobs live on `NtxStrategyConfig`."""

    range_period: int = 24  # prior bars whose high/low define "the recent range"
    bb_period: int = 20  # window of the Bollinger band-width volatility reading
    bb_std_mult: float = 2.0  # band half-width in standard deviations
    squeeze_lookback: int = 80  # bars over which band width is ranked against itself
    # Percentile RANK of the prior bar's band width within `squeeze_lookback`
    # readings of the SAME quantity -- not a percentage of price or of equity,
    # so R4's `*_price_pct` / `*_equity_pct` naming does not apply and the bare
    # `_pct` suffix is deliberately avoided. 100.0 disables the filter, which is
    # the control cell of the sweep.
    squeeze_percentile: float = 30.0
    # The breakout bar's own true range must be at least this multiple of the
    # mean true range of the prior `bb_period` bars. "Breaks out WHILE
    # volatility is expanding" -- without it a squeeze can be left by a drift.
    expansion_tr_mult: float = 1.0


class VolatilityBreakoutStrategy(NtxStrategy):
    """Break of the prior `range_period` extreme, gated on prior compression.

    Both gates are evaluated on bars that closed BEFORE the bar being judged.
    That matters more here than for a plain Donchian: the breakout bar is by
    construction a wide bar, so including it in the band-width reading would
    push the compression measure up and make the squeeze filter reject its own
    signals -- a silent, self-cancelling rule that still produces a plausible
    equity curve.
    """

    def __init__(self, config: VolatilityBreakoutConfig) -> None:
        if config.range_period < 2:
            raise ValueError(f"range_period must be >= 2, got {config.range_period}")
        if config.bb_period < 2:
            raise ValueError(f"bb_period must be >= 2, got {config.bb_period}")
        if config.squeeze_lookback < 2:
            raise ValueError(f"squeeze_lookback must be >= 2, got {config.squeeze_lookback}")
        if not 0.0 < config.squeeze_percentile <= 100.0:
            raise ValueError(
                f"squeeze_percentile must be in (0, 100], got {config.squeeze_percentile}"
            )
        if config.bb_std_mult <= 0:
            raise ValueError(f"bb_std_mult must be > 0, got {config.bb_std_mult}")
        if config.expansion_tr_mult < 0:
            raise ValueError(f"expansion_tr_mult must be >= 0, got {config.expansion_tr_mult}")
        super().__init__(config)
        # Rolling state over COMPLETED bars, per instrument. All of it is
        # maintained in `on_indicators_update`, which the base class runs for
        # warm-up history too -- state mutated in `on_bar_signal` would be
        # mutated twice on a restart (R46/F033).
        self._highs: dict[InstrumentId, list[float]] = {}
        self._lows: dict[InstrumentId, list[float]] = {}
        self._closes: dict[InstrumentId, list[float]] = {}
        self._trs: dict[InstrumentId, list[float]] = {}
        self._widths: dict[InstrumentId, list[float]] = {}

    # ------------------------------------------------------------------ #
    # warm-up
    # ------------------------------------------------------------------ #
    def warmup_bars(self) -> int:
        """Enough bars for the range, the ranked band width AND the ATR.

        `bb_period + squeeze_lookback - 1` bars produce `squeeze_lookback` band
        widths, which is what the percentile is ranked over. The `+1` is the bar
        being judged, which every reading excludes.
        """
        cfg = self.config
        return (
            max(
                cfg.range_period,
                cfg.bb_period + cfg.squeeze_lookback - 1,
                cfg.atr_period,
            )
            + 1
        )

    def on_indicators_init(self, instrument_id: InstrumentId) -> None:
        self._highs[instrument_id] = []
        self._lows[instrument_id] = []
        self._closes[instrument_id] = []
        self._trs[instrument_id] = []
        self._widths[instrument_id] = []

    def on_indicators_update(self, bar: Bar) -> None:
        """Record one completed bar and the two derived series it produces."""
        inst_id = bar.bar_type.instrument_id
        if inst_id not in self._highs:
            self.on_indicators_init(inst_id)
        high, low, close = float(bar.high), float(bar.low), float(bar.close)
        closes = self._closes[inst_id]
        # True range needs the PREVIOUS close; on the first bar of a series
        # there is none, so the bar's own range is the only honest answer.
        prev_close = closes[-1] if closes else None
        tr = (
            high - low
            if prev_close is None
            else max(high - low, abs(high - prev_close), abs(low - prev_close))
        )
        self._highs[inst_id].append(high)
        self._lows[inst_id].append(low)
        closes.append(close)
        self._trs[inst_id].append(tr)
        self._widths[inst_id].append(self._band_width(closes, self.config))

        keep_bars = max(self.config.range_period, self.config.bb_period) + 2
        keep_widths = self.config.squeeze_lookback + 2
        self._highs[inst_id] = self._highs[inst_id][-keep_bars:]
        self._lows[inst_id] = self._lows[inst_id][-keep_bars:]
        self._closes[inst_id] = closes[-keep_bars:]
        self._trs[inst_id] = self._trs[inst_id][-keep_bars:]
        self._widths[inst_id] = self._widths[inst_id][-keep_widths:]

    # ------------------------------------------------------------------ #
    # the three readings
    # ------------------------------------------------------------------ #
    @staticmethod
    def _band_width(closes: list[float], cfg: VolatilityBreakoutConfig) -> float:
        """Bollinger band width of the last `bb_period` closes, as a fraction of
        the mean. NaN until the window is full -- NaN, not 0.0, because a zero
        would rank as the tightest squeeze there has ever been and would open a
        run's first trades on no information at all.
        """
        window = closes[-cfg.bb_period :]
        if len(window) < cfg.bb_period:
            return math.nan
        mean = sum(window) / len(window)
        if mean <= 0:
            return math.nan
        var = sum((c - mean) ** 2 for c in window) / len(window)
        return 2.0 * cfg.bb_std_mult * math.sqrt(var) / mean

    def breakout_range(self, inst_id: InstrumentId) -> tuple[float, float] | None:
        """(highest high, lowest low) of the last `range_period` PRIOR bars.

        The current bar is already recorded by `on_indicators_update`, so it is
        sliced off here; including it would make `close > high` tautological.
        """
        period = self.config.range_period
        highs, lows = self._highs.get(inst_id, []), self._lows.get(inst_id, [])
        if len(highs) < period + 1:
            return None
        return max(highs[-period - 1 : -1]), min(lows[-period - 1 : -1])

    def squeeze_rank(self, inst_id: InstrumentId) -> float | None:
        """Percentile rank of the PRIOR bar's band width among the previous
        `squeeze_lookback` band widths. 0 = quietest in the whole lookback.

        Returns None while the lookback is not full. The current bar is excluded
        on purpose: a breakout bar widens the bands, so measuring compression on
        it would make the filter reject the very signals it is meant to pass.
        """
        n = self.config.squeeze_lookback
        widths = self._widths.get(inst_id, [])
        if len(widths) < n + 1:
            return None
        window = widths[-n - 1 : -1]
        if any(math.isnan(w) for w in window):
            return None
        ref = window[-1]  # band width as of the bar before the one being judged
        return 100.0 * sum(1 for w in window if w <= ref) / len(window)

    def expansion_ratio(self, inst_id: InstrumentId) -> float | None:
        """Current bar's true range over the mean true range of the prior
        `bb_period` bars. > 1 means this bar is louder than the recent norm."""
        n = self.config.bb_period
        trs = self._trs.get(inst_id, [])
        if len(trs) < n + 1:
            return None
        prior = trs[-n - 1 : -1]
        mean = sum(prior) / len(prior)
        if mean <= 0:
            return None
        return trs[-1] / mean

    # ------------------------------------------------------------------ #
    # readiness and signal
    # ------------------------------------------------------------------ #
    def signal_ready(self, inst_id: InstrumentId) -> bool:
        """ "A signal is possible", not "I have counted N bars" (R52/R-01).

        Readiness deliberately requires the squeeze history even when the filter
        is disabled (`squeeze_percentile=100`), so the control cell of the sweep
        judges exactly the same set of bars as the filtered cells. Otherwise the
        control would get a head start and the comparison would be worthless.
        """
        return (
            self.breakout_range(inst_id) is not None
            and self.squeeze_rank(inst_id) is not None
            and self.expansion_ratio(inst_id) is not None
        )

    def on_bar_signal(self, bar: Bar) -> Signal | None:
        inst_id = bar.bar_type.instrument_id
        if not self.signal_ready(inst_id):
            return None
        rng = self.breakout_range(inst_id)
        rank = self.squeeze_rank(inst_id)
        expansion = self.expansion_ratio(inst_id)
        if rng is None or rank is None or expansion is None:  # narrows for the type checker
            return None
        hi, lo = rng
        close = float(bar.close)

        if close <= hi and close >= lo:
            return None  # no break: cheapest test first, it rejects most bars
        if rank > self.config.squeeze_percentile:
            return None  # broke out of noise, not out of a squeeze
        if expansion < self.config.expansion_tr_mult:
            return None  # drifted out of the squeeze rather than expanding out

        direction = Direction.LONG if close > hi else Direction.SHORT
        edge = hi if direction is Direction.LONG else lo
        return Signal(
            str(inst_id),
            direction,
            reason=(
                f"squeeze_break close={close:.6g} edge={edge:.6g} "
                f"rank={rank:.0f} expansion={expansion:.2f}"
            ),
        )
