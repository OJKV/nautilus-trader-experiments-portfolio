"""S004 -- Cross-sectional momentum.

Rank every instrument in the universe by its return over the last `lookback`
bars, and on every `rebalance_bars`-th bar hold the strongest `long_n` (and,
optionally, short the weakest `short_n`).

**Why it exists.** S003 trades a rare event -- a 55-bar channel break -- so its
frequency is whatever the market hands it: about 0.8 entries/day across ten
instruments, in clusters. A ranking rule fires on a *schedule*, so frequency
becomes a parameter rather than a hope: `rebalance_bars` sets how often the
portfolio is re-decided and `long_n + short_n` how many names it wants to hold.
That is the property the owner asked for -- an execution path exercised several
times a day.

**Why it might work** (the economic claim, not a curve-fitting story).
Cross-sectional momentum is a *relative* statement, not a directional one: the
recent winners keep outrunning the recent losers. Unlike a breakout it does not
need a trend to begin, only a dispersion of returns to persist for a few more
days, and crypto majors disperse constantly -- one narrative rotates in while
another bleeds. In the long/short configuration it is also partially
market-neutral, so a broad drawdown reaches it through dispersion rather than
through beta. The documented horizon for this effect is weeks, not minutes,
which is why the lookbacks here are 5-15 days rather than hours.

**What it is NOT.** This is not a full portfolio optimiser. The framework caps
concurrent positions and sizes every entry from risk, so what this class
expresses is *which* names it wants and *when* it wants to decide -- never how
big (R24, `docs/14-extensibility.md` §2).

Signal logic only. Sizing, the stop, the trailing stop, the journal, adoption
and the whole live path are `NtxStrategy`.
"""

from __future__ import annotations

from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import InstrumentId

from ntx.strategies.base import NtxStrategy, NtxStrategyConfig
from ntx.strategies.signal import Direction, Signal

NANOS_PER_SECOND = 1_000_000_000


class CrossSectionalMomentumConfig(NtxStrategyConfig, frozen=True):
    """Knobs of the ranking rule only; every risk and exit knob is inherited.

    None of these is a percentage, so R4's `*_price_pct` / `*_roi_pct` /
    `*_equity_pct` suffixes do not apply -- `lookback` and `rebalance_bars` are
    counts of bars, and the `*_n` fields are counts of instruments.
    """

    lookback: int = 60  # bars of return used to rank
    rebalance_bars: int = 6  # re-decide the held set every N bars
    long_n: int = 3  # hold the strongest N
    short_n: int = 0  # short the weakest N; 0 = long only
    min_ranked: int = 5  # refuse to rank a universe thinner than this
    # Diagnostic control, NOT a second strategy: ranks ascending, i.e. buys the
    # recent losers. Its only job is to tell "there is no cross-sectional
    # structure here" from "there is structure and momentum has the wrong sign".
    reversal: bool = False


class CrossSectionalMomentumStrategy(NtxStrategy):
    """Relative-strength ranking across the whole subscribed universe.

    How the held set changes, which is the part a reader gets wrong:

    * On a rebalance bar an instrument in the top `long_n` emits ``LONG``. If it
      is already held long the framework sees a same-direction signal and simply
      keeps the position (no churn, no second fill).
    * An instrument that falls out of the top set emits **nothing**. It keeps its
      position and is managed by the inherited stop / trailing stop. There is no
      way for a strategy to ask the framework for a plain "close": an opposite
      signal is both the exit *and* a flip into the other side (`base.py` on_bar,
      `exits.py` rule 1). Emitting one for a merely mid-ranked name would open a
      short on an asset the rule has no short opinion about, so it is not done.
    * In the long/short configuration a held long that sinks all the way into the
      bottom `short_n` does emit ``SHORT``, and the framework exits and flips.
      That is a genuine reversal of the rule's opinion, not bookkeeping.

    Consequence to keep in mind when reading results: the held set is "the top N
    as of the last rebalance that had a free slot", not "the top N right now".
    `rejects['max_positions']` in the run counters says how often the cap bound
    (R40).
    """

    def __init__(self, config: CrossSectionalMomentumConfig) -> None:
        if config.lookback < 2:
            raise ValueError(f"lookback must be >= 2, got {config.lookback}")
        if config.rebalance_bars < 1:
            raise ValueError(f"rebalance_bars must be >= 1, got {config.rebalance_bars}")
        if config.long_n < 0 or config.short_n < 0:
            raise ValueError(f"long_n/short_n must be >= 0, got {config.long_n}/{config.short_n}")
        if config.long_n + config.short_n < 1:
            raise ValueError("long_n + short_n must be >= 1, otherwise nothing is ever held")
        if config.min_ranked < config.long_n + config.short_n:
            raise ValueError(
                f"min_ranked ({config.min_ranked}) must cover long_n + short_n "
                f"({config.long_n + config.short_n}); otherwise the long and short "
                f"sets can overlap on a thin universe"
            )
        # R40: `allow_short=False` silently drops every SHORT in `base.on_bar`.
        # A long/short configuration that runs long-only without saying so is
        # exactly the "knob that did nothing" failure, so it is refused here.
        if config.short_n > 0 and not config.allow_short:
            raise ValueError("short_n > 0 requires allow_short=True, or the shorts are dropped")
        # Respect the cap instead of fighting it: if the rule wants more names
        # than the risk layer will hold, the ones that get in are decided by bar
        # ARRIVAL order, not by rank -- a silent lottery that makes a results
        # table meaningless. Ask for a set the cap can actually hold.
        if config.long_n + config.short_n > config.max_concurrent_positions:
            raise ValueError(
                f"long_n + short_n ({config.long_n + config.short_n}) exceeds "
                f"max_concurrent_positions ({config.max_concurrent_positions}); raise the cap "
                f"and lower risk_equity_pct to keep aggregate risk constant, or hold fewer names"
            )
        super().__init__(config)
        # Rolling closes per instrument, and the derived lookback return. Both
        # are filled by `on_indicators_update`, which runs for warm-up history
        # too -- state built inside `on_bar_signal` would be empty for the first
        # `warmup_bars()` live bars after a restart (R46/F033/R-01).
        self._closes: dict[InstrumentId, list[float]] = {}
        self._score: dict[InstrumentId, float] = {}

    # ------------------------------------------------------------------ #
    # subclass contract
    # ------------------------------------------------------------------ #
    def warmup_bars(self) -> int:
        """Enough bars for the lookback return AND the framework's own ATR."""
        return max(self.config.lookback, self.config.atr_period) + 1

    def on_indicators_init(self, instrument_id: InstrumentId) -> None:
        self._closes[instrument_id] = []

    def on_indicators_update(self, bar: Bar) -> None:
        inst_id = bar.bar_type.instrument_id
        if inst_id not in self._closes:
            self.on_indicators_init(inst_id)
        closes = self._closes[inst_id]
        closes.append(float(bar.close))
        keep = self.config.lookback + 2
        if len(closes) > keep:
            del closes[:-keep]
        if len(closes) >= self.config.lookback + 1:
            base = closes[-self.config.lookback - 1]
            if base > 0:
                self._score[inst_id] = closes[-1] / base - 1.0

    def signal_ready(self, instrument_id: InstrumentId) -> bool:
        """A signal is possible: this name is scored and the field is deep enough.

        Not "I have counted N bars" (R52/R-01). A ranking rule needs a *field* to
        rank, so a single warm instrument is not readiness -- the monitor would
        otherwise report ready while the rule still cannot decide anything.
        """
        return instrument_id in self._score and len(self._score) >= self.config.min_ranked

    def on_bar_signal(self, bar: Bar) -> Signal | None:
        inst_id = bar.bar_type.instrument_id
        if not self.signal_ready(inst_id):
            return None
        if not self.is_rebalance_bar(bar):
            return None

        order = self.ranked()
        total = len(order)
        n_long = min(self.config.long_n, total)
        # Longs win a contested slot on a thin field, so the two sets can never
        # overlap and one instrument can never be both wanted long and short.
        n_short = min(self.config.short_n, max(0, total - n_long))

        if inst_id in order[:n_long]:
            return self._signal(inst_id, Direction.LONG, order)
        if n_short and inst_id in order[total - n_short :]:
            return self._signal(inst_id, Direction.SHORT, order)
        return None

    # ------------------------------------------------------------------ #
    # ranking
    # ------------------------------------------------------------------ #
    def score(self, instrument_id: InstrumentId) -> float | None:
        """Return over the last `lookback` CLOSED bars, or None if not yet scored."""
        return self._score.get(instrument_id)

    def ranked(self) -> list[InstrumentId]:
        """Instruments best-first (worst-first when `reversal` is set).

        Ties break on the instrument id, so the order never depends on which
        instrument's bar happened to arrive first -- with ten symbols on one
        timestamp, arrival order is an implementation detail of the data feed
        and must not reach a trading decision.
        """
        sign = 1.0 if self.config.reversal else -1.0
        return [
            inst_id
            for inst_id, _ in sorted(self._score.items(), key=lambda kv: (sign * kv[1], str(kv[0])))
        ]

    def is_rebalance_bar(self, bar: Bar) -> bool:
        """True on bars whose CLOSE sits on the rebalance grid.

        Derived from `ts_event` -- which is the bar's close time (R8/F014) --
        rather than from a per-instrument bar counter. The ten instruments do
        not start on the same day (DOGE is six months behind BTC), so counters
        would drift apart and each name would rebalance on its own private
        schedule, which is not a cross-sectional rule at all. On the grid,
        `rebalance_bars=6` on 4h bars means 00:00 UTC daily, for every name.
        """
        interval_ns = int(bar.bar_type.spec.timedelta.total_seconds() * NANOS_PER_SECOND)
        if interval_ns <= 0:  # defensive: a tick-based bar type has no fixed period
            return False
        return (bar.ts_event // interval_ns) % self.config.rebalance_bars == 0

    def _signal(
        self, inst_id: InstrumentId, direction: Direction, order: list[InstrumentId]
    ) -> Signal:
        return Signal(
            str(inst_id),
            direction,
            reason=(
                f"rank {order.index(inst_id) + 1}/{len(order)} "
                f"ret{self.config.lookback}={self._score[inst_id]:+.4f}"
                f"{' reversal' if self.config.reversal else ''}"
            ),
        )
