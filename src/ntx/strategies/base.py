"""NtxStrategy -- the framework half of every strategy.

A subclass implements `warmup_bars()` and `on_bar_signal()` and nothing else.
Everything below is owned here and inherited unchanged by S002-S006
(docs/14-extensibility.md §2):

  sizing, leverage ceiling, min-notional rejection, the exit stack, venue-side
  protective stops, attempt recording, journal writes, and structured logging.

A `Signal` states intent only -- it cannot set size, leverage, or widen a stop.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators.volatility import AverageTrueRange
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce, TriggerType
from nautilus_trader.model.events import OrderFilled, PositionClosed
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy

from ntx.obs.journal import Journal
from ntx.risk.guards import Verdict, loss_state
from ntx.risk.sizing import SizingLimits, size_position
from ntx.strategies.bars import history_bar_complete
from ntx.strategies.exits import (
    ExitConfig,
    PositionState,
    TrailingStop,
    evaluate_exit,
    initial_stop_price,
)
from ntx.strategies.signal import Direction, ExitReason, Signal
from ntx.strategies.watchdog import (
    data_stalled,
    needs_protection,
    stale_stops,
    stop_topup_needed,
)


def _same_order(a, b) -> bool:
    """Order-id equality that cannot raise (F081).

    Adoption stores `entry_order_id = "adopted"`; nautilus's Cython
    `ClientOrderId` raises TypeError when compared with a str, and a raise in an
    event handler shuts the whole node down (S004, 2026-10-07 04:00).
    """
    return a is not None and b is not None and str(a) == str(b)


class NtxStrategyConfig(StrategyConfig, frozen=True):
    """Shared configuration. Percentages carry their denominator (R4)."""

    instrument_ids: list[str]
    bar_spec: str = "1-HOUR-LAST-EXTERNAL"

    # risk
    risk_equity_pct: float = 0.5
    max_leverage: float = 3.0
    max_concurrent_positions: int = 3
    per_asset_pct_of_budget: float = 40.0

    # exits
    sl_atr_mult: float | None = 2.0
    sl_price_pct: float | None = None
    atr_period: int = 14
    tsl_enabled: bool = True
    tsl_activation_price_pct: float = 1.5
    tsl_trail_price_pct: float = 1.0
    # Move the VENUE stop when the trailing level improves by at least this much
    # of price (G8/B-08). **0 = off, and off is the default**, because it was
    # measured: `experiments/venue_trail.py` at live config puts IS Sharpe at
    # 0.61 off versus 0.27-0.30 on, and OOS at 1.13 off versus 1.00-1.23 on.
    # A venue stop can be taken out intrabar by a wick; the Python trailing stop
    # only acts on a bar close, and that difference costs more than it saves.
    # Drawdown does improve when it is on (OOS 12.6% -> 9.4%), so this is a
    # trade-off, not a dead end -- it is kept behind the flag and off by default
    # so every recorded gate run still describes what actually runs (R47).
    tsl_sync_min_price_pct: float = 0.0
    # How long the venue equity read is reused before another REST call.
    equity_refresh_secs: int = 60
    tp_price_pct: float | None = None
    target_roi_pct: float | None = None
    max_bars_in_position: int | None = None

    # behaviour
    allow_short: bool = True
    equity_override_usd: float | None = None

    # Absolute path to the SQLite journal. Optional: backtests and sweeps run
    # without one. Set for every live/paper session -- it is the only record
    # that survives the VPN dropping, since venue state becomes unreachable and
    # process memory dies with the node (F026).
    journal_path: str | None = None

    # How often to re-verify every position has a live venue stop (R15).
    watchdog_minutes: int = 20

    # Set by node assembly, never by strategy code (R3).
    is_demo: bool = False  # tags journal rows so demo PnL never mixes with live (F028)
    request_warmup: bool = False  # live: fetch history so indicators are warm at start (F029)
    flatten_on_stop: bool = False  # rehearsal only: leave nothing open when the node stops
    # docs/09 §7 latency stress (R8c): act on bar t's decisions at bar t+1's close.
    # Backtest stress only; 0 everywhere else. Venue/backtest stops are unaffected.
    decision_delay_bars: int = 0
    # OKX: stops close the WHOLE position (close_fraction=1). Required for a
    # reduce-only stop to be accepted, and allows one such order per position (F036).
    venue_stop_closes_position: bool = False
    # Create the venue stop WITH the entry (OCO child) instead of after the fill.
    # Measured on demo: protection is live ~38 ms BEFORE the fill vs 449-520 ms
    # after it (G7). Off by default: a venue stop can trigger INTRABAR while the
    # validated S003 backtest exits at bar close, so the gate must be re-run
    # before this becomes the default.
    venue_attaches_stop: bool = False
    # Account loss guards (C-02). 0 disables. Enforced before every entry.
    daily_loss_limit_equity_pct: float = 5.0
    max_drawdown_equity_pct: float = 20.0
    # False = observe only: everything runs, no order is ever sent (G6 checks).
    trading_enabled: bool = True
    # Monitor snapshot (docs/16); None in backtests. Quotes give live marks (R22).
    state_path: str | None = None
    state_interval_secs: int = 15
    subscribe_quotes: bool = False


# A stored high-water mark this many times the current equity is a unit
# mismatch, not a drawdown (N2-01).
HWM_IMPLAUSIBLE_MULT = 5.0

# Adoption runs one position per tick of this timer (L2-04).
ADOPTION_TIMER = "ntx-adoption"


class NtxStrategy(Strategy):
    """Base class. Subclasses provide the signal; the framework does the rest."""

    # One adoption per tick: long enough for the event loop to actually send the
    # previous replacement stop, short enough that the exposure is one submit
    # rather than a minute (L2-04).
    ADOPT_TICK_SECS = 2

    def __init__(self, config: NtxStrategyConfig) -> None:
        super().__init__(config)
        self.exit_cfg = ExitConfig(
            sl_atr_mult=config.sl_atr_mult,
            sl_price_pct=config.sl_price_pct,
            tsl_enabled=config.tsl_enabled,
            tsl_activation_price_pct=config.tsl_activation_price_pct,
            tsl_trail_price_pct=config.tsl_trail_price_pct,
            tp_price_pct=config.tp_price_pct,
            target_roi_pct=config.target_roi_pct,
            max_bars_in_position=config.max_bars_in_position,
        )
        self.limits = SizingLimits(
            risk_equity_pct=config.risk_equity_pct,
            max_leverage=config.max_leverage,
            max_concurrent_positions=config.max_concurrent_positions,
            per_asset_pct_of_budget=config.per_asset_pct_of_budget,
        )
        self.instrument_ids: list[InstrumentId] = [
            InstrumentId.from_str(i) for i in config.instrument_ids
        ]
        self.instruments: dict[InstrumentId, Instrument] = {}
        self.atr: dict[InstrumentId, AverageTrueRange] = {}
        self.states: dict[InstrumentId, PositionState] = {}
        self.stop_orders: dict[InstrumentId, object] = {}
        # Protective stops fill at the VENUE, closing the position without ever
        # passing through `_exit`. Without this map those exits are invisible to
        # `exit_counts` -- the first backtest showed 146 trades but only 55
        # counted exits, and the 91 stop-outs were the whole missing mass.
        self.stop_client_order_ids: dict[object, InstrumentId] = {}
        self._adopt_queue: list[tuple] = []
        self.orphans_closed = 0
        self._adopt_timer_on = False
        self.bars_seen: dict[InstrumentId, int] = {}
        # Last bar ts processed per instrument: a history response can overlap
        # the first live bar, and a repeated bar would corrupt rolling state.
        self._last_bar_ts: dict[InstrumentId, int] = {}
        self._pending_signal: Signal | None = None

        # counters surfaced to the health checks and the backtest report
        self.rejects: dict[str, int] = {}
        # Caps that SHRANK an order rather than refusing it. Without this a
        # 1x run and a 3x run look identical in a results table while one of
        # them is quietly trading smaller (R40).
        self.size_caps: dict[str, int] = {}
        self.exit_counts: dict[str, int] = {}
        # Mark-to-market equity, sampled once per distinct bar timestamp.
        # The account report only updates on account EVENTS (fills), so an
        # equity curve built from it omits unrealised PnL entirely -- which
        # makes drawdown and every risk-adjusted metric wrong (F015).
        self.equity_curve: list[tuple[int, float]] = []
        self._last_equity_ts: int = -1

        self.entries_attempted = 0
        self.entries_submitted = 0
        self.unprotected_events = 0
        self.entries_refused = 0
        self.stops_refused = 0

        self.journal: Journal | None = (
            Journal(Path(config.journal_path)) if config.journal_path else None
        )
        self._trade_seq = 0
        # Closed trades still receiving their closing fills, per instrument:
        # the journal row is re-priced from real fills and fees (F038).
        self._closing: dict[InstrumentId, dict] = {}
        # Flip re-entries waiting for the old position to close (review H1).
        self._pending_entry: dict[InstrumentId, tuple] = {}
        self._delayed: dict[InstrumentId, tuple] = {}  # R8c latency stress
        # Wall-clock arrival of the last LIVE bar per instrument (stall detection).
        self._last_live_bar_ns: dict[InstrumentId, int] = {}
        self._live_bars = 0
        # History warm-up bookkeeping (F040): live bars that arrive before the
        # history reply are buffered, then replayed once history is ingested.
        self._live_buffer: dict[InstrumentId, list[Bar]] = {}
        self._awaiting_since: dict[InstrumentId, int] = {}
        self._history_requested_ns: dict[InstrumentId, int] = {}
        self._history_req: dict[object, InstrumentId] = {}
        self.entry_skips: dict[str, int] = {}
        # Loss guards (C-02): day resets at UTC midnight, HWM is monotonic.
        self._day: str | None = None
        self._day_start_equity: float = 0.0
        self._high_water: float = 0.0
        # A journal mark seeded while equity was unknown; checked on first read (N3-03).
        self._hwm_unchecked: float | None = None
        self._killed = False
        # Exits in flight: close order id -> instrument (C-01). The position
        # state AND its venue stop are kept until the close is confirmed.
        self._close_orders: dict[object, InstrumentId] = {}
        self.close_failures = 0
        # Set by node assembly: cancels a venue stop the cache cannot see and
        # returns its trigger price (F044). None in backtests.
        self.venue_stop_hook = None
        # The read-only half: same lookup, no cancel. Adoption plans with this so
        # nothing is cancelled before it is about to be replaced (L-10).
        self.venue_stop_peek_hook = None
        # Cancels close-position stops that guard nothing (F065).
        self.venue_stop_reap_hook = None
        # Reads venue positions; checks a "closed while offline" (F066).
        self.venue_position_hook = None
        # Venue positions this node has no state for (F067).
        self.unmanaged_venue_positions = 0
        # Instruments the venue says are held while the cache says flat: no
        # entry until that is resolved, or one position becomes two (F068).
        self._venue_blocked: set[str] = set()
        # Cache positions the venue does not have; not unprotected, just unreal.
        self._phantom_instruments: set[str] = set()
        # Set by node assembly on a LIVE/demo node: returns the venue's own
        # equity figure, because an exchange balance is not equity (T2-01).
        # None in backtests, where the account balance IS equity.
        self.equity_hook = None
        self.account_label: str | None = None
        self._equity_cache: float | None = None
        self._equity_cache_ns: int = 0

    # ------------------------------------------------------------------ #
    # subclass contract
    # ------------------------------------------------------------------ #
    def warmup_bars(self) -> int:
        raise NotImplementedError

    def on_bar_signal(self, bar: Bar) -> Signal | None:
        raise NotImplementedError

    def on_indicators_init(self, instrument_id: InstrumentId) -> None:
        """Create any subclass indicators for this instrument."""

    def on_indicators_update(self, bar: Bar) -> None:
        """Feed subclass indicators. Called before `on_bar_signal`."""

    def signal_ready(self, instrument_id: InstrumentId) -> bool:
        """Can this instrument produce a signal yet?

        Counting bars is not the same as being ready: S003 counted 61 bars and
        reported `warm: true` while its 55-bar channel was still empty (R-01).
        Subclasses that keep their own rolling state override this.
        """
        return self.bars_seen.get(instrument_id, 0) >= self.warmup_bars()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        for inst_id in self.instrument_ids:
            instrument = self.cache.instrument(inst_id)
            if instrument is None:
                self.log.error(f"{inst_id} not in cache -- not subscribing")
                continue
            self.instruments[inst_id] = instrument
            self.atr[inst_id] = AverageTrueRange(self.config.atr_period)
            self.bars_seen[inst_id] = 0
            self.on_indicators_init(inst_id)
            bar_type = BarType.from_str(f"{inst_id}-{self.config.bar_spec}")
            self.subscribe_bars(bar_type)
            if self.config.subscribe_quotes:
                self.subscribe_quote_ticks(inst_id)
            if self.config.request_warmup:
                # History arrives via `on_historical_data`: indicators warm up,
                # nothing trades. +5 bars of slack for venue gaps.
                lookback = bar_type.spec.timedelta * (self.warmup_bars() + 5)
                self._await_history(inst_id, requested_ns=self.clock.timestamp_ns())
                now = self.clock.utc_now()
                rid = self.request_bars(
                    bar_type,
                    start=now - lookback,
                    # Explicit window; the probe returned exactly the bars asked for.
                    end=now,
                    callback=self._on_history_response,
                )
                self._history_req[rid] = inst_id
        self._seed_risk_state()
        self._adopt_positions()
        # Ask the venue what it really holds NOW, not at the first watchdog tick
        # twenty minutes from now. `positions_unprotected` skips instruments the
        # venue says are not there, and that set is empty until this runs -- so a
        # node whose cache holds phantoms reported them as naked positions for
        # twenty minutes, which is exactly the window a deploy verifies in. It
        # failed two live deploys in a row on an account that was flat.
        try:
            self._check_venue_inventory()
        except Exception as e:  # never fatal: this is a reporting check
            self.log.warning(f"could not take the venue inventory at start: {e}")
        # Judge the account NOW, not at the first watchdog tick. `_seed_risk_state`
        # has just restored the high-water mark from the journal (R55/B-04), and
        # `_risk_check` is otherwise wired only to the protection watchdog, which
        # fires every `watchdog_minutes` -- twenty. Measured in the guard drill on
        # 2026-09-24: a node restarted with a $1,000 mark, $700 of equity and a real
        # open position traded on happily for the three minutes it was watched, and
        # would have kept going for seventeen more. After adoption, so a KILL can
        # close the positions it has just taken over.
        try:
            self._risk_check()
        except Exception as e:
            self.log.error(f"start-up risk check failed: {e}")
        if self.config.state_path:
            self.clock.set_timer(
                name="ntx-state",
                interval=timedelta(seconds=self.config.state_interval_secs),
                callback=lambda _e=None: self.write_state(),
            )
        self.log.info(f"equity for sizing: ${self._equity_usd():,.2f}")
        self.clock.set_timer(
            name="ntx-protection-watchdog",
            interval=timedelta(minutes=self.config.watchdog_minutes),
            callback=self._protection_watchdog,
        )
        self.log.info(
            f"started on {len(self.instruments)} instruments, "
            f"warmup={self.warmup_bars()} bars, exits={self.exit_cfg}"
        )
        if self.config.state_path:
            self.log.info(f"state snapshot: {self.config.state_path}")
        # A crash-restart loop is otherwise invisible: the restart gap is shorter
        # than the snapshot staleness limit, so the alerter never notices (O-05).
        self._jevent(
            "node_started",
            severity="warning",
            instruments=len(self.instruments),
            killed=self._killed,
        )

    def on_stop(self) -> None:
        if self._last_equity_ts >= 0:
            self._append_equity(self._last_equity_ts, self._equity_at(self._last_equity_ts))
        if self.config.flatten_on_stop and not self.config.is_demo:
            # A rehearsal tool (S900, demo). On a live account it would close
            # every position on a restart -- refuse rather than trust the flag.
            self.log.error("flatten_on_stop is demo-only; refusing on a live account")
        elif self.config.flatten_on_stop:
            # Through `_flatten`, never `close_all_positions`: that closes the
            # CACHE, and closing a phantom opens a real position (R74, P2).
            for inst_id in self.instruments:
                self._flatten(inst_id)
        self.log.info(
            f"entries attempted={self.entries_attempted} submitted={self.entries_submitted} "
            f"rejects={self.rejects} size_caps={self.size_caps} exits={self.exit_counts} "
            f"unprotected_events={self.unprotected_events}"
        )

    # ------------------------------------------------------------------ #
    # bar handling
    # ------------------------------------------------------------------ #
    def _record_equity(self, bar: Bar) -> None:
        """Sample mark-to-market equity once per bar timestamp.

        Instruments at one timestamp arrive one after another, so when the
        first bar of timestamp T arrives the others still carry T-1 prices.
        Sampling then mixed timestamps (review C4). Instead, the arrival of T
        closes the books for T-1: equity is re-marked with every open
        position priced at its T-1 close and recorded under T-1.
        """
        ts = bar.ts_event
        if ts == self._last_equity_ts:
            return
        prev, self._last_equity_ts = self._last_equity_ts, ts
        if prev >= 0:
            self._append_equity(prev, self._equity_at(prev))

    def _equity_at(self, ts: int) -> float | None:
        venue = self.instrument_ids[0].venue
        eq = self.portfolio.equity(venue)
        if not eq:
            return None
        total = sum(float(v) for v in eq.values())
        unreal_now = self.portfolio.unrealized_pnls(venue) or {}
        total -= sum(float(v) for v in unreal_now.values())
        for pos in self.cache.positions_open(strategy_id=self.id):
            bt = BarType.from_str(f"{pos.instrument_id}-{self.config.bar_spec}")
            price = None
            for i in (0, 1):
                b = self.cache.bar(bt, index=i)
                if b is not None and b.ts_event <= ts:
                    price = b.close
                    break
            if price is None:
                price = self.cache.bar(bt).close if self.cache.bar(bt) else None
            if price is not None:
                total += float(pos.unrealized_pnl(price))
        return total

    def _risk_equity_for_journal(self) -> float | None:
        """The number the JOURNAL stores -- which must be the number the guard
        compares (N2-01).

        `equity_curve` is mark-to-market across every currency, which is right
        for the metrics (R36) and wrong as a risk basis: on the demo account it
        values coin dust at $5,101 while the guard reasons about a small balance of USDT.
        `_seed_risk_state` reads the journal after a restart, so storing the
        wrong basis manufactured a 99.3% drawdown and flattened the book on
        2026-09-19. One definition, or none.
        """
        return self._equity_or_none()

    def _append_equity(self, ts: int, total: float | None) -> None:
        if total is None:
            return
        self.equity_curve.append((ts, total))
        # Sample sparsely into the journal: a 4h backtest over six years would
        # otherwise write ~13k rows, and the equity curve is already in memory.
        #
        # ALWAYS write the first sample of a process, though. `equity_curve`
        # starts empty in every new process, so "every 6th" meant one row per 24h
        # on 4h bars -- and a node restarted more often than that (a deploy, a
        # crash, a reboot) never wrote one at all. Measured on the live account:
        # zero rows after 18h52m and five bar closes, leaving `_seed_risk_state`
        # with no high-water mark to restore. R55 says risk state that gates money
        # survives a restart; this is the row it survives in.
        n = len(self.equity_curve)
        if self.journal and (n == 1 or n % 6 == 0):
            risk_equity = self._risk_equity_for_journal()
            if risk_equity is None:
                return
            self._jwrite(
                "record_equity",
                lambda: self.journal.record_equity(risk_equity, open_positions=len(self.states)),
            )

    # Reconciliation is asynchronous. Measured on the box: 4 s too early on the
    # restart that broke, 6 s and correct on the next one. Look again before
    # believing the cache.
    VENUE_FLAT_ATTEMPTS = 5
    VENUE_FLAT_WAIT_S = 3.0

    def _sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)

    def _confirm_venue_flat(self, instruments) -> list:
        """Which of these the VENUE agrees are flat, so the row may be closed.

        `_adopt_positions` reads open positions from nautilus's cache, and the
        cache is filled by a reconciliation task that `on_start` does not wait
        for. On 2026-09-26 the strategy started four seconds into that task, saw
        an empty cache, and wrote `closed_while_offline` over a SOL position that
        was open at OKX. The instrument then looked free, the next signal opened a
        second long on top of the first, and `net_mode` merged them (F066).

        L-09 settled this for stops -- "the venue did not answer" is not "there is
        no stop". It matters more for positions: a stop wrongly believed absent is
        replaced, a position wrongly believed closed is opened again on top of
        itself.

        The first version of this check raised. It was right about the venue and
        wrong about what to do: the node exited, systemd restarted it into the
        same race ten times, `StartLimitBurst` tripped and the soak was down for
        65 minutes (F068). A guard that turns a transient disagreement into a
        permanent outage has become the thing it was written to prevent.

        So: look again (reconciliation usually finishes), and if the venue still
        disagrees, degrade instead of dying -- keep the journal row open and
        refuse entries on that one instrument. The danger was never "a row is
        open"; it was "an instrument looks free while a position is live on it".
        Nine other instruments keep trading and every open position stays managed.
        """
        hook = getattr(self, "venue_position_hook", None)
        if hook is None or not instruments:
            return list(instruments)
        wanted = {str(i) for i in instruments}
        held: dict = {}
        for attempt in range(1, self.VENUE_FLAT_ATTEMPTS + 1):
            try:
                held = hook() or {}
            except Exception as e:
                self.log.error(
                    f"cannot confirm at the venue that {sorted(wanted)} are flat ({e}) -- "
                    f"keeping their journal rows open and refusing entries on them"
                )
                self._jevent(
                    "adoption_false_flat",
                    severity="critical",
                    reason=f"venue unreadable while confirming {sorted(wanted)} flat: {e}",
                )
                self._venue_blocked |= wanted
                return []
            disputed = {
                i
                for i in wanted
                if abs(float(held.get(i.removesuffix(".OKX"), held.get(i, 0.0)) or 0.0)) > 0
            }
            if not disputed:
                return sorted(wanted)
            if attempt < self.VENUE_FLAT_ATTEMPTS:
                self.log.warning(
                    f"the venue still holds {sorted(disputed)} while the cache reports flat "
                    f"-- reconciliation may still be running; looking again "
                    f"(attempt {attempt}/{self.VENUE_FLAT_ATTEMPTS})"
                )
                self._sleep(self.VENUE_FLAT_WAIT_S)
        self.log.error(
            f"the venue holds {sorted(disputed)} while the cache reports flat, after "
            f"{self.VENUE_FLAT_ATTEMPTS} attempts. NOT journalling them as closed, and "
            f"refusing entries on them: an instrument that looks free while a position is "
            f"live on it is how one position becomes two (F066)."
        )
        self._jevent(
            "adoption_false_flat",
            severity="critical",
            reason=(
                f"venue holds {sorted(disputed)} but the cache reports flat; rows kept open "
                f"and entries blocked on them"
            ),
        )
        self._venue_blocked |= disputed
        return sorted(wanted - disputed)

    def _reap_orphan_stops(self, held) -> None:
        """Cancel close-position stops left on instruments the venue says are flat.

        A position that disappears while we are down takes its journal row with
        it (`closed_while_offline`) but not its venue stop: adoption only looks
        at instruments that still HOLD something, and a flat instrument is never
        peeked. The stop then sits at OKX guarding nothing until the next
        position on that instrument tries to place its own and is refused
        "You can only place 1 TP/SL order to close an entire position" (F065).

        Never fatal. A stop we could not cancel costs a refused stop later --
        which `_replace_stale_stop` now repairs -- while refusing to start costs
        every position we hold.
        """
        reap = getattr(self, "venue_stop_reap_hook", None)
        if reap is None:
            return
        if not self._venue_writes_allowed("cancel stops that guard no position"):
            return
        try:
            cancelled = reap(held)
        except Exception as e:
            self.log.warning(f"could not sweep stops that guard no position: {e}")
            self._jevent("orphan_stop_sweep_failed", severity="warning", reason=str(e))
            return
        if cancelled:
            self.log.warning(f"cancelled {len(cancelled)} venue stop(s) guarding no position")
            self._jevent(
                "orphan_stops_reaped",
                severity="warning",
                reason=f"cancelled {cancelled} -- left over from positions that closed while down",
            )

    def _adopt_positions(self) -> None:
        """Take over positions held at start (gap G1). Venue = what, journal = why."""
        from ntx.live.reconcile import AdoptAction, VenuePosition, adoption_plan

        venue, stops, stop_orders = [], {}, {}
        for inst_id in self.instruments:
            for pos in self.cache.positions_open(instrument_id=inst_id):
                live_stop = next(
                    (
                        o
                        for o in self.cache.orders_open(instrument_id=inst_id)
                        if o.order_type.name in ("STOP_MARKET", "STOP_LIMIT")
                    ),
                    None,
                )
                if live_stop is not None:
                    stops[str(inst_id)] = float(live_stop.trigger_price)
                    stop_orders[str(inst_id)] = live_stop
                venue.append(
                    VenuePosition(
                        str(inst_id),
                        float(pos.signed_qty),
                        float(pos.avg_px_open),
                        live_stop is not None,
                    )
                )
        venue = self._venue_checked(venue)
        # An orphan blocks the NEXT entry's stop, so it goes before anything
        # else is planned -- and it is only ever about instruments with no
        # position, so it cannot touch what adoption is about to take over.
        self._reap_orphan_stops({vp.instrument_id for vp in venue if vp.quantity})

        # PLANNING IS READ-ONLY (L-10). The old code cancelled every pre-restart
        # stop up front and re-placed them later in the plan loop, so with two
        # positions the first was naked for the whole of the second one's REST
        # round trip. Nothing is cancelled here; the cancel happens next to the
        # re-place, one instrument at a time, in `_adopt`.
        released = {}
        peek = self.venue_stop_peek_hook or self.venue_stop_hook
        if peek is not None:
            for vp in venue:
                if not (vp.quantity and not vp.has_protective_stop):
                    continue
                try:
                    price = peek(vp.instrument_id)
                except Exception as e:
                    # L-09: "the venue did not answer" is NOT "there is no stop".
                    # Treating them alike let a failed lookup flatten a healthy,
                    # protected position. The venue stop is still in place, so
                    # refusing to start is the safe move -- systemd retries, and
                    # the stale-snapshot alert fires if it keeps failing.
                    self._jevent(
                        "adoption_lookup_failed",
                        severity="critical",
                        instrument=vp.instrument_id,
                        reason=str(e),
                    )
                    raise RuntimeError(
                        f"cannot read the venue stop for {vp.instrument_id} ({e}). "
                        f"Refusing to adopt on unreliable information: the position keeps "
                        f"its existing venue stop and nothing was sent."
                    ) from e
                if price is not None:
                    released[vp.instrument_id] = price
        rows = []
        if self.journal:
            wanted = {str(i) for i in self.instruments}
            rows = [dict(r) for r in self.journal.open_trades() if r["instrument_id"] in wanted]
        plan = adoption_plan(venue, rows, stops, released)
        # Every "venue flat: closed while offline" is checked against the venue
        # before a single row is closed (F066). What comes back is the subset the
        # venue agrees is flat; the rest keep their rows and are blocked (F068).
        closable = set(
            self._confirm_venue_flat(
                [a.instrument_id for a in plan if a.action is AdoptAction.CLOSE_JOURNAL]
            )
        )
        plan = [
            a
            for a in plan
            if a.action is not AdoptAction.CLOSE_JOURNAL or a.instrument_id in closable
        ]
        if not plan:
            return
        if not self.config.trading_enabled:
            for a in plan:
                self.log.warning(
                    f"OBSERVE: adoption would {a.action} {a.instrument_id}: {a.reason}"
                )
            return
        by_id = {str(i): i for i in self.instruments}
        self._adopt_queue = []
        for a in plan:
            inst_id = by_id[a.instrument_id]
            self.log.warning(f"ADOPTION {a.action} {inst_id}: {a.reason}")
            if self.journal:
                self.journal.event(
                    f"adoption_{a.action}", severity="warning", instrument=a.instrument_id,
                    reason=a.reason,
                )  # fmt: skip
            if a.action is AdoptAction.FLATTEN:
                self._flatten(inst_id)
            elif a.action is AdoptAction.CLOSE_JOURNAL:
                try:
                    if self.journal.trade_state(a.trade_id) == "submitted":
                        self.journal.transition(a.trade_id, "open")
                    self.journal.transition(a.trade_id, "closing")
                    self.journal.transition(
                        a.trade_id, "closed", exit_reason="closed_while_offline"
                    )
                except Exception as e:
                    self.log.error(f"journal close failed for {a.trade_id}: {e}")
            else:
                # Queued, not adopted here: see _drain_adoptions (L2-04).
                self._adopt_queue.append((inst_id, a, stop_orders.get(a.instrument_id)))
        self._drain_adoptions()

    def _venue_checked(self, cached: list) -> list:
        """Cross-check the cache's positions against the venue before adopting (F073).

        Adoption built its list from the cache alone, so a phantom there with a
        same-side journal row was adopted: OKX refused its stop (nothing to
        protect), STOP_REFUSED exited, and the exit sent F069's trade. Only what
        the venue holds on the same side is adopted, at the venue's size (R27).
        An unreadable venue keeps the cache's list -- every close now asks the
        venue itself (`_venue_close_qty`), so that is no longer a path to an order.
        """
        from ntx.live.reconcile import VenuePosition

        hook = getattr(self, "venue_position_hook", None)
        if hook is None:
            return cached
        try:
            held = hook() or {}
        except Exception as e:
            self.log.warning(f"adoption could not cross-check the cache against the venue: {e}")
            return cached
        checked = []
        for vp in cached:
            key = vp.instrument_id.removesuffix(".OKX")
            qty = float(held.get(key, held.get(vp.instrument_id, 0.0)) or 0.0)
            if qty and (qty > 0) == (vp.quantity > 0):
                checked.append(
                    VenuePosition(vp.instrument_id, qty, vp.entry_price, vp.has_protective_stop)
                )
                continue
            reason = f"the cache holds {vp.quantity} but the venue holds {qty or 'nothing'}"
            self.log.error(f"ADOPTION skipped {vp.instrument_id}: {reason} (phantom, F069)")
            self._jevent(
                "adoption_phantom_skipped",
                severity="critical",
                instrument=vp.instrument_id,
                reason=reason,
            )
        return checked

    def _drain_adoptions(self, _event=None) -> None:
        """Adopt ONE position per event-loop turn, then schedule the next.

        `_submit_stop` does not send anything. It calls `submit_order`, and
        nautilus's `LiveRiskEngine.execute` only *enqueues*; the queue is drained
        by a task on the same event loop that `on_start` is blocking. Adopting
        every position inline therefore produced

            cancel A, queue A, cancel B, queue B, cancel C, queue C, return

        and nothing reached the venue until `on_start` returned -- so A was naked
        for the whole of B's and C's release round trips, which is precisely what
        L-10 was meant to end. Worse, a release that raised left the earlier
        queued stops unsent.

        One adoption per tick hands control back to the loop between them, so each
        replacement is actually sent before the next stop is cancelled. A position
        still waiting its turn is not exposed at all: it keeps its pre-restart
        venue stop until the moment it is adopted (L-10).
        """
        if not self._adopt_queue:
            self._cancel_adoption_timer()
            return
        inst_id, a, live_stop = self._adopt_queue.pop(0)
        try:
            self._adopt(inst_id, a, live_stop)
        finally:
            if self._adopt_queue:
                self._schedule_adoption_timer()
            else:
                self._cancel_adoption_timer()

    def _schedule_adoption_timer(self) -> None:
        if self._adopt_timer_on:
            return
        self.clock.set_timer(
            name=ADOPTION_TIMER,
            interval=timedelta(seconds=self.ADOPT_TICK_SECS),
            callback=self._drain_adoptions,
        )
        self._adopt_timer_on = True

    def _cancel_adoption_timer(self) -> None:
        """A timer left running would re-enter adoption for the life of the node."""
        if not self._adopt_timer_on:
            return
        try:
            self.clock.cancel_timer(ADOPTION_TIMER)
        except Exception as e:  # already fired and gone: not worth failing start-up
            self.log.warning(f"could not cancel {ADOPTION_TIMER}: {e}")
        self._adopt_timer_on = False

    def _adopt(self, inst_id, a, live_stop) -> None:
        instrument = self.instruments[inst_id]
        units = a.quantity * float(instrument.multiplier)
        trailing = (
            TrailingStop(
                a.direction,
                a.entry_price,
                self.exit_cfg.tsl_activation_price_pct,
                self.exit_cfg.tsl_trail_price_pct,
            )
            if self.exit_cfg.tsl_enabled
            else None
        )
        # A venue stop away from this trade's own stop (review 2026-10-11 F-03):
        # with the venue trail off, the venue stop never moves, so a different
        # price means it belongs to an earlier trade -- re-place it at ours (R71).
        # With the trail on, a moved stop is expected; say so and keep it.
        own_stop = getattr(a, "journal_stop", None)
        trail_on = float(getattr(self.config, "tsl_sync_min_price_pct", 0.0) or 0.0) > 0
        stop_price = own_stop if own_stop is not None and not trail_on else a.stop_price
        if own_stop is not None and trail_on:
            self._jevent(
                "adoption_stop_diverges", severity="warning", instrument=str(inst_id),
                reason=f"venue stop {a.stop_price} vs this trade's {own_stop}; trail sync on",
            )  # fmt: skip
        trade_id = a.trade_id
        if trade_id is None and self.journal:
            trade_id = f"t-adopt-{uuid.uuid4().hex[:8]}"
            self.journal.open_trade(
                trade_id=trade_id, correlation_id="adopted", strategy_id=str(self.id),
                venue=str(inst_id.venue), instrument_id=str(inst_id), side=str(a.direction),
                qty=a.quantity, entry_price=a.entry_price, stop_price=a.stop_price,
                is_demo=1 if self.config.is_demo else 0,
            )  # fmt: skip
        state = PositionState(
            direction=a.direction,
            entry_price=a.entry_price,
            stop_price=stop_price,
            margin_usd=units * a.entry_price / max(self.config.max_leverage, 1e-9),
            trailing=trailing,
            base_units=units,
            meta={
                "trade_id": trade_id,
                "entry_order_id": "adopted",  # every later fill is a closing fill
                "qty": str(a.quantity),
                "filled_qty": a.quantity,
                "covered_qty": 0.0,
                "entry_qty": a.quantity,
                "entry_notional": a.quantity * a.entry_price,
            },
        )
        self.states[inst_id] = state
        if live_stop is not None:
            self.stop_orders[inst_id] = live_stop
            self.stop_client_order_ids[live_stop.client_order_id] = inst_id
            state.meta["covered_qty"] = a.quantity
            if stop_price != a.stop_price:
                self._replace_stale_stop(inst_id, state, a.stop_price, stop_price)
            return
        # The pre-restart venue stop (invisible to the cache on OKX, F044) is
        # cancelled HERE and replaced on the next line -- not minutes earlier
        # while other instruments were still being planned (L-10).
        if a.releases_venue_stop and self.venue_stop_hook is not None:
            try:
                self.venue_stop_hook(str(inst_id))
            except Exception as e:
                self._jevent(
                    "adoption_release_failed",
                    severity="critical",
                    instrument=str(inst_id),
                    reason=str(e),
                )
                raise RuntimeError(
                    f"cannot release the old venue stop for {inst_id} ({e}); "
                    f"refusing to place a second one"
                ) from e
        self._submit_stop(inst_id, state, a.quantity)

    def _sync_venue_stop(self, inst_id, state: PositionState) -> None:
        """Move the stop AT THE VENUE up to the trailing level (B-08/G8).

        The trailing stop used to live only in Python, evaluated once per 4h bar,
        while the venue stop stayed at its initial 2xATR level for the life of
        the trade. With the node down -- a deploy, a crash, a reboot -- a trade
        that was +15% could give all of it back.

        OKX allows exactly one close-position stop per position (F036), so this
        is a cancel followed immediately by a place, in that order and with
        nothing in between (L-10). If the place fails, the watchdog re-attaches
        within `watchdog_minutes` and `positions_unprotected` reports it.
        """
        trailing = state.trailing
        if trailing is None or not trailing.armed or not self.exit_cfg.tsl_enabled:
            return
        level = trailing.level
        if level is None:
            return
        threshold = float(getattr(self.config, "tsl_sync_min_price_pct", 0.0) or 0.0)
        if threshold <= 0:
            return
        current = float(state.stop_price)
        # "Better" means tighter, and only ever tighter: the trailing stop is
        # monotonic, and a venue stop that loosens is a way to give back profit.
        improvement = (level - current) if state.direction is Direction.LONG else (current - level)
        if improvement <= 0:
            return
        if (improvement / max(abs(current), 1e-9)) * 100.0 < threshold:
            return
        if not self._venue_writes_allowed(f"move the {inst_id} stop to {level:.6g}"):
            return
        self.log.info(
            f"TRAIL {inst_id} venue stop {current:.6g} -> {level:.6g} "
            f"(+{improvement / max(abs(current), 1e-9) * 100.0:.2f}% tighter) "
            f"corr={state.meta.get('corr')}"
        )
        self._cancel_protective_stop(inst_id)
        state.stop_price = level
        self._submit_stop(inst_id, state, float(state.meta.get("filled_qty") or 0.0))
        self._jevent(
            "stop_trailed", severity="info", instrument=str(inst_id), reason=f"{level:.6g}"
        )

    def _mark(self, inst_id) -> tuple[float | None, str]:
        """Live mark for monitoring: mid quote if subscribed, else last bar close."""
        q = self.cache.quote_tick(inst_id)
        if q is not None:
            return (float(q.bid_price) + float(q.ask_price)) / 2.0, "quote"
        return self._last_close(inst_id), "bar"

    def write_state(self) -> None:
        """Atomic JSON snapshot for the read-only monitor. Never raises."""
        if not self.config.state_path:
            return
        try:
            from ntx.obs.atomic import atomic_write_json

            now = self.clock.timestamp_ns()
            venue = self.instrument_ids[0].venue
            unreal = self.portfolio.unrealized_pnls(venue) or {}
            positions = []
            for inst_id, st in self.states.items():
                mark, source = self._mark(inst_id)
                upnl = None
                for pos in self.cache.positions_open(instrument_id=inst_id):
                    if mark is not None:
                        upnl = float(pos.unrealized_pnl(self.instruments[inst_id].make_price(mark)))
                stop = self.stop_orders.get(inst_id)
                positions.append(
                    {
                        "instrument_id": str(inst_id),
                        "side": st.direction.name,
                        "qty": float(st.meta.get("filled_qty") or 0.0),
                        "entry_price": self._entry_px(st),
                        "mark_price": mark,
                        "mark_source": source,
                        "stop_price": st.stop_price,
                        "trailing_stop": st.trailing.level if st.trailing else None,
                        "bars_held": st.bars_held,
                        "unrealized_usd": upnl,
                        "protected": stop is not None and self._stop_is_live(stop),
                        "trade_id": st.meta.get("trade_id"),
                    }
                )
            instruments = []
            for inst_id in self.instruments:
                live = self._last_live_bar_ns.get(inst_id)
                seen = self.bars_seen.get(inst_id, 0)
                instruments.append(
                    {
                        "id": str(inst_id),
                        "last_close": self._last_close(inst_id),
                        "last_bar_ts_ns": self._last_bar_ts.get(inst_id),
                        "live_bar_age_s": (now - live) / 1e9 if live else None,
                        "bars_seen": seen,
                        "warm": seen >= self.warmup_bars(),
                        # "warm" counts bars; "ready" means a signal is possible.
                        "ready": self.signal_ready(inst_id),
                    }
                )
            atomic_write_json(
                Path(self.config.state_path),
                {
                    "schema": 1,
                    "ts": self.clock.utc_now().isoformat(),
                    "ts_ns": now,
                    "strategy_id": str(self.config.state_path).rsplit("/", 1)[-1].split("-")[0],
                    "trader_id": str(self.trader_id),
                    "venue": str(venue),
                    "bar_spec": self.config.bar_spec,
                    "is_demo": self.config.is_demo,
                    "trading_enabled": self.config.trading_enabled,
                    # `equity_usd` is the SIZING equity (kept for older readers).
                    # None when unknown: a stale venue read is not a $0 account (T3-06).
                    "equity_usd": self._equity_or_none(),
                    "sizing_equity_usd": self._equity_or_none(),
                    "account_equity_usd": self._account_equity_or_none(),
                    # e.g. "sub-account 'nautilus-demo'": which account these numbers
                    # are from, set by node assembly (F059). None in backtests.
                    "account_label": getattr(self, "account_label", None),
                    "equity_override_usd": self.config.equity_override_usd,
                    "unrealized_usd": sum(float(v) for v in unreal.values()),
                    "positions_unprotected": self.positions_unprotected,
                    # A KILL-latched bot read green everywhere without this.
                    "killed": bool(self._killed),
                    "instruments": instruments,
                    "positions": positions,
                    "counters": {
                        "live_bars": self._live_bars,
                        "entries_attempted": self.entries_attempted,
                        "entries_submitted": self.entries_submitted,
                        "unprotected_events": self.unprotected_events,
                        "entries_refused": self.entries_refused,
                        "stops_refused": self.stops_refused,
                        "entry_skips": dict(self.entry_skips),
                        "rejects": dict(self.rejects),
                        "size_caps": dict(self.size_caps),
                        "exits": dict(self.exit_counts),
                    },
                },
            )
        except Exception as e:  # monitoring must never stop trading
            self.log.error(f"state snapshot failed: {e!r}")

    def _await_history(self, inst_id, requested_ns: int) -> None:
        self._history_requested_ns[inst_id] = requested_ns
        self._awaiting_since[inst_id] = self.clock.timestamp_ns()
        self._live_buffer[inst_id] = []

    def _on_history_response(self, request_id) -> None:
        inst_id = self._history_req.pop(request_id, None)
        if inst_id is not None:
            self._history_complete(inst_id)

    def _history_complete(self, inst_id) -> None:
        """History is in: replay any live bars that arrived meanwhile (F040)."""
        self._awaiting_since.pop(inst_id, None)
        buffered = self._live_buffer.pop(inst_id, [])
        seen, need = self.bars_seen.get(inst_id, 0), self.warmup_bars()
        if seen < need:
            self.log.warning(f"{inst_id} warmup_short: history gave {seen} of {need} bars")
            if self.journal:
                self.journal.event(
                    "warmup_short",
                    severity="warning",
                    instrument=str(inst_id),
                    seen=seen,
                    need=need,
                )
        self.log.info(
            f"history ready {inst_id}: bars_seen={self.bars_seen.get(inst_id, 0)}, "
            f"replaying {len(buffered)} buffered live bar(s)"
        )
        for bar in buffered:
            self.on_bar(bar)

    def on_historical_data(self, data) -> None:
        """Warm-up bars from `request_bars`: update state, never trade (F029)."""
        if not isinstance(data, Bar) or data.bar_type.instrument_id not in self.atr:
            return
        # OKX includes the still-forming bar; ingesting it would feed a partial
        # close and then drop the final bar as a duplicate (F034).
        interval = data.bar_type.spec.timedelta.total_seconds() * 1_000_000_000
        # Judged at REQUEST time: a bar still forming when we asked is partial,
        # however late the reply is handled (F040).
        asked = self._history_requested_ns.get(
            data.bar_type.instrument_id, self.clock.timestamp_ns()
        )
        if not history_bar_complete(data.ts_event, int(interval), asked):
            return
        self._ingest(data)

    def _ingest(self, bar: Bar) -> bool:
        """Feed indicators and signal state. False if the bar was already seen."""
        inst_id = bar.bar_type.instrument_id
        if bar.ts_event <= self._last_bar_ts.get(inst_id, -1):
            return False
        self._last_bar_ts[inst_id] = bar.ts_event
        self.atr[inst_id].handle_bar(bar)
        self.on_indicators_update(bar)
        self.bars_seen[inst_id] = self.bars_seen.get(inst_id, 0) + 1
        if self.bars_seen[inst_id] >= self.warmup_bars():
            # Stateful signals (e.g. Donchian's rolling channel) must see
            # every bar, history included; the result is discarded here.
            self._pending_signal = self.on_bar_signal(bar)
        else:
            self._pending_signal = None
        return True

    def on_bar(self, bar: Bar) -> None:
        inst_id = bar.bar_type.instrument_id
        if inst_id not in self._last_live_bar_ns:
            self.log.info(f"first live bar {inst_id} ts_event={bar.ts_event} close={bar.close}")
        self._last_live_bar_ns[inst_id] = self.clock.timestamp_ns()
        self._live_bars += 1
        buffer = self._live_buffer.get(inst_id)
        if buffer is not None:
            buffer.append(bar)  # history not in yet (F040)
            return
        if not self._ingest(bar):
            self.log.warning(f"{inst_id} bar ts_event={bar.ts_event} ignored as already seen")
            return

        try:
            self._record_equity(bar)
        except Exception as e:  # monitoring must never stop trading
            self.log.error(f"equity sample failed: {e!r}")

        if self.bars_seen[inst_id] < self.warmup_bars():
            return

        signal = self._pending_signal
        direction = signal.direction if signal else None
        if direction is Direction.SHORT and not self.config.allow_short:
            direction = None

        state = self.states.get(inst_id)
        price = float(bar.close)

        # R8c latency stress: what was decided one bar ago is done now, at this
        # close; this bar's own decision waits for the next one.
        if int(getattr(self.config, "decision_delay_bars", 0) or 0) > 0:
            todo = self._delayed.pop(inst_id, None)
            if state is not None:
                state.bars_held += 1
                reason = evaluate_exit(state, price, self.exit_cfg, signal_direction=direction)
                if reason is not None:
                    self._delayed[inst_id] = ("exit", reason, direction)
                else:
                    self._sync_venue_stop(inst_id, state)
                if todo and todo[0] == "exit":
                    _, old_reason, old_dir = todo
                    flip = old_reason is ExitReason.OPPOSITE_SIGNAL and old_dir is not None
                    if flip:
                        self._pending_entry[inst_id] = (old_dir, bar)
                    self._delayed.pop(inst_id, None)
                    self._exit(inst_id, old_reason)
                    if flip and inst_id in self._pending_entry:
                        self._flip(inst_id, *self._pending_entry.pop(inst_id))
                return
            if todo and todo[0] == "enter":
                self._enter(inst_id, todo[1], bar)
                return
            if direction not in (None, Direction.FLAT):
                self._delayed[inst_id] = ("enter", direction)
            return

        # --- manage an open position first --------------------------------
        if state is not None:
            state.bars_held += 1
            reason = evaluate_exit(state, price, self.exit_cfg, signal_direction=direction)
            if reason is not None:
                flip = reason is ExitReason.OPPOSITE_SIGNAL and direction is not None
                if flip:
                    # Registered BEFORE the exit: in a backtest the close fills
                    # synchronously inside _exit and fires on_position_closed.
                    self._pending_entry[inst_id] = (direction, bar)
                self._exit(inst_id, reason)
                if flip and inst_id in self._pending_entry:
                    self._flip(inst_id, *self._pending_entry.pop(inst_id))
                return
            # Kept the position: `evaluate_exit` has just advanced the trailing
            # level on this close, so the venue stop follows it here (B-08).
            self._sync_venue_stop(inst_id, state)
            return

        # A flip that never saw its close by the next bar is abandoned (R20).
        if self._pending_entry.pop(inst_id, None) is not None:
            self.log.warning(f"{inst_id} flip abandoned: old position did not close in one bar")

        # --- no position: consider an entry -------------------------------
        if direction in (None, Direction.FLAT):
            return
        self._enter(inst_id, direction, bar)

    # ------------------------------------------------------------------ #
    # entry / exit
    # ------------------------------------------------------------------ #
    def _equity_or_none(self) -> float | None:
        """Account EQUITY, or None when the venue has not told us yet.

        "Unknown" and "zero" are opposite facts: `guards.py` maps 0 to KILL, so
        a missing balance during a reconnect used to flatten the book (B-07).

        On a live OKX account `balance_total(USDT)` is **available cash**, not
        equity: opening a position moves its isolated margin out of that number
        (drill A1: $8.08 of notional at 3x took the total from 4999.751 to
        4997.054, exactly the $2.693 margin plus the taker fee) and it never
        moves with price while the position is open -- one value repeated 319
        times in the 09-18 soak. Used as a risk basis that reads a normal book
        as a loss: three positions of ~$3 margin on a small balance look like a 26%
        drawdown, which is KILL, flatten and latch on a healthy account (T2-01).

        A backtest account does not behave that way -- nothing leaves the
        balance -- so the difference belongs in node assembly, not here (R3):
        the live node installs `equity_hook`, which reads the venue's own equity
        figure. With no hook this is exactly the account balance, which is what
        every recorded backtest used.
        """
        if self.config.equity_override_usd:
            return float(self.config.equity_override_usd)
        if self.equity_hook is not None:
            venue_equity = self._hooked_equity()
            if venue_equity is not None:
                return venue_equity
            # Fall through: a stale hook is not a reason to invent a number.
            return None
        account = self.portfolio.account(self.instrument_ids[0].venue)
        if account is None:
            return None
        total = account.balance_total(USDT)
        return float(total.as_double()) if total is not None else None

    def _account_equity_or_none(self) -> float | None:
        """What the account REALLY holds, ignoring the sizing override.

        For display only. On the demo soak the sizing equity is deliberately the
        real account balance while the demo account holds ~a sample amount of play
        money; a monitor that shows only one of them leaves the operator asking
        which account they are looking at.
        """
        if self.equity_hook is not None:
            return self._hooked_equity()
        account = self.portfolio.account(self.instrument_ids[0].venue)
        if account is None:
            return None
        total = account.balance_total(USDT)
        return float(total.as_double()) if total is not None else None

    def _hooked_equity(self) -> float | None:
        """The venue's own equity, cached: this is a REST call, and the guard is
        consulted far more often than the number can meaningfully change."""
        now = self.clock.timestamp_ns()
        fresh_for = self.config.equity_refresh_secs * 1_000_000_000
        if self._equity_cache is not None and (now - self._equity_cache_ns) < fresh_for:
            return self._equity_cache
        try:
            value = self.equity_hook()
        except Exception as e:
            value = None
            self.log.error(f"venue equity read failed: {e}")
        if value is not None and value > 0:
            self._equity_cache, self._equity_cache_ns = float(value), now
            return self._equity_cache
        # Keep serving the last good value for a while; past that, "unknown",
        # which the guard maps to PAUSE rather than KILL (B-07).
        if self._equity_cache is not None and (now - self._equity_cache_ns) < 3 * fresh_for:
            return self._equity_cache
        return None

    def _equity_usd(self) -> float:
        equity = self._equity_or_none()
        return 0.0 if equity is None else equity

    def _jwrite(self, what: str, fn) -> None:
        """Run a journal write that must never raise (O-04).

        A full disk, an I/O error or a locked database turned every repair,
        flatten and kill path into an exception, because the journal call sat in
        front of the safety action. The journal is analytics; the venue is truth
        (R27).
        """
        if self.journal is None:
            return
        try:
            fn()
        except Exception as e:
            self.log.error(f"journal write failed ({what}): {e}")

    def _jevent(self, kind: str, **kw) -> None:
        self._jwrite(kind, lambda: self.journal.event(kind, **kw))

    # ------------------------------------------------------------------ #
    # risk state that must survive a restart (B-05)
    # ------------------------------------------------------------------ #
    def _kill_latch_path(self) -> Path | None:
        if not self.config.state_path:
            return None
        return Path(str(self.config.state_path) + ".killed")

    def _latch_kill(self) -> None:
        """Record the KILL on disk. A restart used to clear it silently, so the
        account could take the full drawdown limit again (B-05)."""
        path = self._kill_latch_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.clock.utc_now().isoformat() if hasattr(self, "clock") else "1")
        except Exception as e:
            self.log.error(f"cannot write the kill latch: {e}")

    def _seed_risk_state(self) -> None:
        """Seed the high-water mark from the journal and re-arm a latched KILL.

        Both lived in memory only: after a restart an account already 19% down
        could lose another 20% before KILL, repeatedly, and a KILL was forgotten
        entirely (B-05).
        """
        equity_now = self._equity_or_none()
        if self.journal is not None:
            try:
                hwm = self.journal.equity_high_water_mark()
            except Exception as e:
                hwm, _ = None, self.log.error(f"cannot read the high-water mark: {e}")
            if hwm:
                hwm = float(hwm)
                # A stored mark wildly out of scale with the account is a unit
                # mismatch, not a 99% loss. Refuse it loudly rather than acting
                # on it -- acting on it is what flattened the book (N2-01).
                if equity_now is None:
                    # Unknown equity cannot vouch for the mark (N3-03): an
                    # unchecked one is exactly N2-01's false KILL. Hold it back;
                    # `_loss_verdict` checks it against the first real reading,
                    # and judges nothing before that (unknown -> PAUSE).
                    self._hwm_unchecked = hwm
                    self.log.warning(
                        f"high-water mark ${hwm:,.2f} held back: equity unknown at start"
                    )
                elif hwm > equity_now * HWM_IMPLAUSIBLE_MULT:
                    self.log.error(
                        f"REFUSING a high-water mark of ${hwm:,.2f} against ${equity_now:,.2f} "
                        f"of equity: that is {hwm / equity_now:.0f}x, which means the stored "
                        f"basis differs from the guard's. Starting from the current equity."
                    )
                    self._jevent(
                        "hwm_implausible",
                        severity="critical",
                        reason=f"stored={hwm:.2f} equity={equity_now:.2f}",
                    )
                else:
                    self._high_water = max(self._high_water, hwm)
                    self.log.info(
                        f"high-water mark seeded from the journal: ${self._high_water:,.2f}"
                    )
        path = self._kill_latch_path()
        if path is not None and path.exists():
            self._killed = True
            self.log.error(
                f"KILL is latched ({path}): entries stay blocked. "
                f"Clear it deliberately with `python -m ntx.ops.unkill` -- deleting the file "
                f"alone re-KILLs at the next risk check, because the high-water mark is "
                f"still above the account."
            )

    def _risk_check(self, _event=None) -> None:
        """Judge the account away from the entry path (B-04).

        `_loss_verdict` used to have exactly one caller, inside `_enter`, so a
        25% drawdown did nothing until some other instrument produced a
        breakout -- which can be days. docs/08 promises "drawdown -> flatten".
        """
        verdict, why = self._loss_verdict()
        if verdict is Verdict.KILL and not self._killed:
            self._kill_all(why)

    def _kill_all(self, why: str) -> None:
        # KILL is the only state that closes positions (docs/08 ladder).
        self._killed = True
        self.log.error(f"KILL SWITCH: {why} -- flattening every position")
        self._latch_kill()
        self._jevent("guard_KILL", severity="critical", reason=why)
        for held in list(self.states):
            self._exit(held, ExitReason.KILL_SWITCH)

    def _loss_verdict(self) -> tuple[Verdict, str]:
        """Update day-start equity / high-water mark and judge the account (C-02)."""
        equity = self._equity_or_none()
        if equity is None or equity <= 0:
            # Never a KILL: a transient missing balance is not a wiped account.
            return Verdict.PAUSE, "account equity unknown -- skipping entries"
        pending = getattr(self, "_hwm_unchecked", None)
        if pending is not None:
            self._hwm_unchecked = None
            if pending > equity * HWM_IMPLAUSIBLE_MULT:
                self.log.error(
                    f"REFUSING the held-back high-water mark ${pending:,.2f} against "
                    f"${equity:,.2f} of equity: a different basis, not a loss (N3-03)."
                )
                self._jevent(
                    "hwm_implausible",
                    severity="critical",
                    reason=f"stored={pending:.2f} equity={equity:.2f}",
                )
            else:
                self._high_water = max(self._high_water, pending)
        day = self.clock.utc_now().strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self._day_start_equity = day, equity
        self._high_water = max(self._high_water, equity)
        return loss_state(
            day_start_equity=self._day_start_equity,
            high_water=self._high_water,
            equity=equity,
            daily_loss_pct=self.config.daily_loss_limit_equity_pct,
            max_dd_pct=self.config.max_drawdown_equity_pct,
        )

    def _guard_blocks_entry(self, inst_id) -> bool:
        if self._killed:
            # A latched KILL blocks entries on its own: it survived a restart
            # precisely so that it would (N2-02).
            self.entry_skips["killed"] = self.entry_skips.get("killed", 0) + 1
            self.log.error(f"{inst_id} entry_skipped: KILL is latched")
            return True
        verdict, why = self._loss_verdict()
        if verdict is Verdict.OK:
            return False
        key = "killed" if verdict is Verdict.KILL else "paused"
        self.entry_skips[key] = self.entry_skips.get(key, 0) + 1
        self.log.error(f"{inst_id} entry_skipped: {verdict} -- {why}")
        self._jevent(f"guard_{verdict}", severity="critical", instrument=str(inst_id), reason=why)
        if verdict is Verdict.KILL and not self._killed:
            self._kill_all(why)
        return True

    def _count_size_caps(self, sized) -> None:
        """Record every cap that shrank this order (R40)."""
        detail = getattr(sized, "detail", "") or ""
        if not detail.startswith("capped by "):
            return
        for name in detail[len("capped by ") :].split(" + "):
            key = name.strip()
            if key:
                self.size_caps[key] = self.size_caps.get(key, 0) + 1

    def _open_notional_usd(self) -> float:
        """Open notional at the latest close, not at entry (review H2)."""
        total = 0.0
        for inst_id, st in self.states.items():
            px = self._last_close(inst_id)
            total += abs(float(st.base_units)) * (px if px is not None else st.entry_price)
        return total

    def _enter(self, inst_id: InstrumentId, direction: Direction, bar: Bar) -> None:
        if str(inst_id) in self._venue_blocked:
            # The venue says it holds this instrument while our cache says flat
            # (F068). Entering now is how one position becomes two: the first is
            # invisible to us, the second merges into it under `net_mode`, and the
            # stop covers a size nothing budgeted for.
            self.entry_skips["venue_blocked"] = self.entry_skips.get("venue_blocked", 0) + 1
            self.log.error(
                f"{inst_id} entry_skipped: the venue holds a position here that this node "
                f"cannot see. Refusing to open a second one on top of it."
            )
            return
        if self.cache.positions_open(instrument_id=inst_id):
            # We hold no state here, yet the cache holds a position: a phantom
            # (reconciliation saw a close without its opener) or an orphan. Under
            # NETTING our fill lands on that same position id, so an entry nets
            # against it: equal size closes it in the cache, nautilus emits
            # PositionClosed, and a real venue position is left with no state and
            # no stop (P1, live ADA 2026-10-06). Refuse; it costs this
            # instrument's entries until the cache is right again.
            self.entry_skips["cache_not_flat"] = self.entry_skips.get("cache_not_flat", 0) + 1
            phantom = str(inst_id) in getattr(self, "_phantom_instruments", set())
            self.log.error(
                f"{inst_id} entry_skipped: the cache holds a "
                f"{'phantom ' if phantom else ''}position this node has no state for; "
                f"an entry would net against it"
            )
            self._jevent(
                "entry_refused_cache_not_flat",
                severity="warning",
                instrument=str(inst_id),
                reason="phantom" if phantom else "orphan",
            )
            return
        instrument = self.instruments[inst_id]
        atr = self.atr[inst_id]
        if not atr.initialized or atr.value <= 0:
            # Never silent (R40): rehearsal #7 lost an entry here with no trace.
            self.entry_skips["atr_not_ready"] = self.entry_skips.get("atr_not_ready", 0) + 1
            self.log.warning(
                f"{inst_id} entry_skipped: ATR not ready "
                f"(initialized={atr.initialized}, value={atr.value}, "
                f"bars_seen={self.bars_seen.get(inst_id, 0)})"
            )
            return

        price = float(bar.close)
        try:
            stop_price = initial_stop_price(direction, price, self.exit_cfg, float(atr.value))
        except ValueError as e:  # R18: never trade on bad inputs
            self.log.warning(f"{inst_id} stop calculation failed: {e}")
            return
        stop_distance = abs(price - stop_price)

        if self._guard_blocks_entry(inst_id):
            return
        if not self.config.trading_enabled:
            self.entry_skips["observe_only"] = self.entry_skips.get("observe_only", 0) + 1
            self.log.info(f"OBSERVE: would enter {direction} {inst_id} @~{bar.close}")
            return

        self.entries_attempted += 1  # R19: record the attempt first
        attempt_id = f"a-{uuid.uuid4().hex[:10]}"
        self._jwrite(
            "record_attempt",
            lambda: self.journal.record_attempt(
                attempt_id=attempt_id,
                strategy_id=str(self.id),
                instrument_id=str(inst_id),
                side=str(direction),
            ),
        )
        sized = size_position(
            instrument=instrument,
            price=price,
            stop_distance=stop_distance,
            equity_usd=self._equity_usd(),
            limits=self.limits,
            open_positions=len(self.states),
            open_notional_usd=self._open_notional_usd(),
        )
        if not sized.ok:
            self.rejects[str(sized.reject)] = self.rejects.get(str(sized.reject), 0) + 1
            # Visible in the file log: a silent reject storm is how a bot "never
            # trades" with no explanation (R40).
            self.log.warning(f"{inst_id} entry rejected: {sized.reject} {sized.detail}")
            self._jwrite(
                "resolve_attempt",
                lambda: self.journal.resolve_attempt(attempt_id, "rejected", str(sized.reject)),
            )
            self._jevent(
                "entry_rejected",
                severity="info",
                instrument=str(inst_id),
                reject=str(sized.reject),
                detail=sized.detail,
            )
            return

        trailing = (
            TrailingStop(
                direction,
                price,
                self.exit_cfg.tsl_activation_price_pct,
                self.exit_cfg.tsl_trail_price_pct,
            )
            if self.exit_cfg.tsl_enabled
            else None
        )
        self._count_size_caps(sized)
        # The state exists BEFORE the order reaches the venue (B-01): the entry
        # fill can arrive during the very next await, and a fill with no state is
        # routed to the exit path and ignored -- no stop is ever placed, and the
        # watchdog (which iterates `self.states`) cannot see the position.
        self.states[inst_id] = PositionState(
            direction=direction,
            entry_price=price,
            stop_price=stop_price,
            margin_usd=sized.notional_usd / max(self.config.max_leverage, 1e-9),
            bars_held=0,
            trailing=trailing,
            base_units=float(sized.base_units),
            meta={
                "corr": uuid.uuid4().hex[:8],
                "qty": str(sized.quantity),
                "entry_order_id": None,
                "filled_qty": 0.0,
                # An attached stop covers the whole order from the start.
                "covered_qty": float(sized.quantity) if self._stop_attached_at_entry() else 0.0,
            },
        )
        if self.journal:
            self._trade_seq += 1
            trade_id = f"t-{self._trade_seq:06d}-{uuid.uuid4().hex[:6]}"
            self.states[inst_id].meta["trade_id"] = trade_id
        try:
            entry_order_id = self._submit_entry(
                inst_id, direction, float(sized.quantity), stop_price
            )
        except Exception:
            self.states.pop(inst_id, None)
            raise
        self.states[inst_id].meta["entry_order_id"] = entry_order_id
        self.entries_submitted += 1
        self._jwrite(
            "resolve_attempt", lambda: self.journal.resolve_attempt(attempt_id, "submitted")
        )
        if self.journal:
            self._jwrite("open_trade", lambda: self.journal.open_trade(
                trade_id=trade_id,
                correlation_id=self.states[inst_id].meta["corr"],
                strategy_id=str(self.id),
                venue=str(inst_id.venue),
                instrument_id=str(inst_id),
                side=str(direction),
                qty=float(sized.quantity),
                entry_price=price,
                notional_usd=sized.notional_usd,
                risk_usd=sized.risk_usd,
                stop_price=stop_price,
                leverage_eff=sized.effective_leverage,
                equity_before=self._equity_usd(),
                is_demo=1 if self.config.is_demo else 0,
                # 'open' only once the venue confirms a fill (F035)
                state="submitted",
            ))  # fmt: skip
        self.log.info(
            f"ENTRY {direction} {inst_id} qty={sized.quantity} @~{price} "
            f"stop={stop_price:.6g} notional=${sized.notional_usd:.2f} "
            f"risk=${sized.risk_usd:.4f} "
            # ids in the line itself: one grep follows a trade end to end (docs/23 §5)
            f"corr={self.states[inst_id].meta['corr']} "
            f"trade={self.states[inst_id].meta.get('trade_id')} "
            f"order={entry_order_id}"
        )

    def _flip(self, inst_id, direction: Direction, bar) -> None:
        """Enter the opposite side only once the old position is gone (review H1).

        Live, the close confirmation arrives after anything sent next; entering
        at once let `on_position_closed` wipe the NEW position's state and stop.
        """
        if self.cache.positions_open(instrument_id=inst_id):
            self._pending_entry[inst_id] = (direction, bar)
            return
        self._enter(inst_id, direction, bar)

    def _exit(self, inst_id: InstrumentId, reason: ExitReason) -> None:
        """Start an exit. The position is NOT forgotten here (C-01).

        The old code popped the state and cancelled the venue stop before the
        closing order was confirmed, so a rejected, lost or partially filled
        close left a naked position that the watchdog could not see (it iterates
        `self.states`). Now the stop stays until the close is confirmed, and
        `_close_confirmed` does the forgetting.
        """
        state = self.states.get(inst_id)
        if state is None or state.meta.get("closing"):
            return
        self.exit_counts[str(reason)] = self.exit_counts.get(str(reason), 0) + 1
        state.meta["closing"] = str(reason)
        state.meta["close_attempts"] = 0
        state.meta["closed_qty"] = 0.0
        state.meta["close_notional"] = 0.0
        state.meta["close_fees"] = 0.0
        if self.journal and state.meta.get("trade_id"):
            try:
                trade_id = state.meta["trade_id"]
                if self.journal.trade_state(trade_id) == "submitted":
                    self.journal.transition(trade_id, "open")
                if self.journal.trade_state(trade_id) == "open":
                    self.journal.transition(trade_id, "closing")
            except Exception as e:
                self.log.error(f"journal closing transition failed: {e}")
        self.log.info(
            f"EXIT {reason} {inst_id} (closing) corr={state.meta.get('corr')} "
            f"trade={state.meta.get('trade_id')}"
        )
        self._submit_close(inst_id, state)

    def _submit_close(self, inst_id, state: PositionState) -> None:
        if not self._venue_writes_allowed(f"close {inst_id}"):
            return
        qty = self._venue_close_qty(inst_id, state)
        if qty is None:
            return  # refused and journalled; the stop stays, the watchdog retries
        if qty <= 0:
            self._close_confirmed(inst_id, state)
            return
        instrument = self.instruments[inst_id]
        side = OrderSide.SELL if state.direction is Direction.LONG else OrderSide.BUY
        order = self.order_factory.market(
            instrument_id=inst_id,
            order_side=side,
            quantity=instrument.make_qty(qty),
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
        )
        state.meta["close_attempts"] = int(state.meta.get("close_attempts", 0)) + 1
        state.meta["close_order_id"] = order.client_order_id
        self._close_orders[order.client_order_id] = inst_id
        self.submit_order(order)

    def _venue_close_qty(self, inst_id, state: PositionState) -> float | None:
        """How much an exit may close: what the VENUE holds on our side (F073).

        R74 used to hold only in `_flatten`. This path -- Donchian, TSL, KILL,
        STOP_REFUSED, the watchdog retry -- sized its market order from the cache,
        so a phantom there still sent F069's trade: adoption takes a phantom with a
        same-side journal row, OKX refuses its stop, STOP_REFUSED exits, and the
        "close" opens a position. Reduce-only did not stop F069 on this account.

        Returns the quantity to close; 0.0 when the venue is flat (the exit is
        finished -- R49, the venue said so); None when nothing may be sent: the
        venue holds the other side, or could not be read (R70). No hook (a
        backtest) keeps the cache as the truth, as it is there by construction.
        """
        hook = getattr(self, "venue_position_hook", None)
        if hook is None:
            return self._position_qty(inst_id) or float(state.meta.get("filled_qty") or 0.0)
        try:
            held = hook() or {}
            key = str(inst_id).removesuffix(".OKX")
            venue_qty = float(held.get(key, held.get(str(inst_id), 0.0)) or 0.0)
        except Exception as e:
            reason = f"could not read the venue before closing {inst_id} ({e})"
        else:
            if not venue_qty:
                return 0.0
            ours = (venue_qty > 0) == (state.direction is Direction.LONG)
            if ours:
                return abs(venue_qty)
            reason = (
                f"we are {state.direction.name} but the venue holds {venue_qty}; "
                f"a close would add to that position, not close ours"
            )
        self.log.error(f"REFUSING TO CLOSE {inst_id}: {reason}")
        self._jevent(
            "phantom_flatten_refused",
            severity="critical",
            instrument=str(inst_id),
            reason=reason,
        )
        return None

    def _position_qty(self, inst_id) -> float:
        return sum(abs(float(p.quantity)) for p in self.cache.positions_open(instrument_id=inst_id))

    def _record_close_fill(self, event) -> None:
        """A fill of our closing order; finalise once the position is flat."""
        inst_id = self._close_orders.get(event.client_order_id)
        state = self.states.get(inst_id) if inst_id is not None else None
        if state is None:
            return
        qty, px = float(event.last_qty), float(event.last_px)
        if qty > 0 and px > 0:
            state.meta["closed_qty"] = float(state.meta.get("closed_qty", 0.0)) + qty
            state.meta["close_notional"] = float(state.meta.get("close_notional", 0.0)) + qty * px
            state.meta["close_fees"] = float(state.meta.get("close_fees", 0.0)) + self._fee(event)
        if self._position_qty(inst_id) <= 1e-12:
            self._close_confirmed(inst_id, state)

    def _close_confirmed(self, inst_id, state: PositionState) -> None:
        """The venue says flat: now forget the position and cancel the stop."""
        self.states.pop(inst_id, None)
        self._close_orders = {k: v for k, v in self._close_orders.items() if v != inst_id}
        closed = float(state.meta.get("closed_qty") or 0.0)
        price = (float(state.meta["close_notional"]) / closed) if closed else None
        reason = state.meta.get("closing") or str(ExitReason.KILL_SWITCH)
        self._close_journal_trade(
            inst_id,
            state,
            reason,
            exit_price=price,
            exit_fee=float(state.meta.get("close_fees") or 0.0),
        )
        self._cancel_protective_stop(inst_id)
        self.log.info(
            f"CLOSED {reason} {inst_id} @ {price if price else 'n/a'} "
            f"corr={state.meta.get('corr')} trade={state.meta.get('trade_id')}"
        )

    def _close_refused(self, inst_id, state: PositionState, how: str, reason: str) -> None:
        """The venue refused our close. Keep the stop, retry, then halt loudly."""
        self.close_failures += 1
        attempts = int(state.meta.get("close_attempts", 0))
        self.log.error(f"{inst_id} close {how} ({attempts}): {reason} -- position stays protected")
        if self.journal:
            self.journal.event(
                "close_refused", severity="critical", instrument=str(inst_id), reason=reason
            )
        if attempts >= 3:
            state.meta["close_halted"] = True
            self.log.error(f"{inst_id} close failed {attempts}x -- MANUAL ACTION NEEDED")

    # ------------------------------------------------------------------ #
    # protective stops -- F007: the adapter cannot attach at entry, so the
    # stop is submitted immediately on fill and a watchdog repairs any gap.
    # ------------------------------------------------------------------ #
    def _last_close(self, inst_id) -> float | None:
        bar = self.cache.bar(BarType.from_str(f"{inst_id}-{self.config.bar_spec}"))
        return float(bar.close) if bar is not None else None

    @staticmethod
    def _fee(event) -> float:
        fee = getattr(event, "commission", None)
        if fee is None:
            return 0.0
        return float(fee.as_double()) if hasattr(fee, "as_double") else float(fee)

    @staticmethod
    def _slippage_bps(direction: Direction, reference: float | None, fill: float) -> float | None:
        """What the fill cost against the price the signal was taken at, in bps.

        POSITIVE is always adverse -- paying more than the signal price for a
        long, receiving less for a short -- so an average over trades reads as a
        cost. Price improvement stays negative rather than being clipped, or the
        average would only ever count the bad fills.

        The gate charges 5 bps per side at live config; nothing measured the real
        number until now (O-06f).
        """
        try:
            ref = float(reference)
        except (TypeError, ValueError):
            return None
        if ref <= 0:
            return None
        move = (float(fill) - ref) / ref * 10_000.0
        return move if direction is Direction.LONG else -move

    def _record_entry_fill(self, state: PositionState, event, trade_id) -> None:
        """Accumulate an entry fill and record what it actually cost."""
        qty_f, px_f = float(event.last_qty), float(event.last_px)
        state.meta["entry_qty"] = state.meta.get("entry_qty", 0.0) + qty_f
        state.meta["entry_notional"] = state.meta.get("entry_notional", 0.0) + qty_f * px_f
        state.meta["fees"] = state.meta.get("fees", 0.0) + self._fee(event)
        if not (self.journal and trade_id):
            return
        updates = {
            "entry_price": self._entry_px(state),
            "fees_usd": state.meta["fees"],
        }
        # `state.entry_price` is the signal bar's close -- the price the size was
        # computed from. The average fill against it is the slippage.
        slip = self._slippage_bps(state.direction, state.entry_price, self._entry_px(state))
        if slip is not None:
            updates["slippage_bps"] = slip
        try:
            self.journal.update_trade(trade_id, **updates)
        except Exception as e:
            self.log.error(f"journal entry update failed for {trade_id}: {e}")

    @staticmethod
    def _entry_px(state: PositionState) -> float:
        q = state.meta.get("entry_qty", 0.0)
        return state.meta["entry_notional"] / q if q else state.entry_price

    @staticmethod
    def _pnl(state: PositionState, exit_px: float, qty: float | None = None) -> float:
        """Gross PnL in USD. `qty` is in order units (contracts); None = whole position."""
        if qty is None:
            units = state.base_units
        else:
            ordered = float(state.meta.get("qty") or 0.0)
            units = qty * (state.base_units / ordered if ordered else 1.0)
        return (exit_px - NtxStrategy._entry_px(state)) * state.direction.sign * units

    def _forget_closed(self, inst_id, state: PositionState, reason, price=None) -> None:
        self._close_journal_trade(inst_id, state, reason, exit_price=price)

    def _close_journal_trade(
        self,
        inst_id,
        state: PositionState,
        reason,
        exit_price: float | None = None,
        exit_fee: float = 0.0,
    ) -> None:
        """Close the journal row for a position, tolerating a missing one.

        With no `exit_price` the closing fills have not happened yet: the row is
        written provisionally at the last close and re-priced by
        `_record_exit_fill` as the fills arrive (F038).

        Wrapped in try/except because a journal write must never take down a
        live trading node -- but the failure is logged, not swallowed silently.
        """
        if not self.journal:
            return
        trade_id = state.meta.get("trade_id")
        if not trade_id:
            return
        fees = state.meta.get("fees", 0.0) + exit_fee
        if exit_price is None:
            last = self._last_close(inst_id)
            exit_price = last if last is not None else state.entry_price
            self._closing[inst_id] = {
                "trade_id": trade_id,
                "state": state,
                "qty": 0.0,
                "notional": 0.0,
                "fees": fees,
            }
        pnl = self._pnl(state, exit_price)
        try:
            if self.journal.trade_state(trade_id) == "submitted":
                self.journal.transition(trade_id, "open")
            if self.journal.trade_state(trade_id) == "open":
                # `_exit` may already have moved it to `closing` (C-01).
                self.journal.transition(trade_id, "closing")
            self.journal.transition(
                trade_id,
                "closed",
                exit_price=exit_price,
                exit_reason=str(reason),
                pnl_gross_usd=pnl,
                fees_usd=fees,
                pnl_net_usd=pnl - fees,
                bars_held=state.bars_held,
                tsl_armed=1 if (state.trailing and state.trailing.armed) else 0,
                equity_after=self._equity_usd(),
            )
        except Exception as e:
            self.log.error(f"journal close failed for {trade_id}: {e}")

    def _record_exit_fill(self, event) -> None:
        """Re-price a closed trade from one of its closing fills (F038)."""
        c = self._closing.get(event.instrument_id)
        if c is None or not self.journal:
            return
        qty, px = float(event.last_qty), float(event.last_px)
        if qty <= 0 or px <= 0:
            return
        c["qty"] += qty
        c["notional"] += qty * px
        c["fees"] += self._fee(event)
        state = c["state"]
        exit_px = c["notional"] / c["qty"]
        gross = self._pnl(state, exit_px, c["qty"])
        try:
            self.journal.update_trade(
                c["trade_id"],
                exit_price=exit_px,
                fees_usd=c["fees"],
                pnl_gross_usd=gross,
                pnl_net_usd=gross - c["fees"],
            )
        except Exception as e:
            self.log.error(f"journal exit re-price failed for {c['trade_id']}: {e}")
        if c["qty"] >= state.meta.get("filled_qty", 0.0) - 1e-12:
            self._closing.pop(event.instrument_id, None)

    def on_order_filled(self, event: OrderFilled) -> None:
        inst_id = event.instrument_id

        # Did OUR protective stop just fill? Then this is a venue-side stop-out.
        if event.client_order_id in self.stop_client_order_ids:
            # The stop may fill in pieces (review C1). The first piece closes
            # the trade; every piece, this one included, re-prices it. Later
            # pieces arrive with the id already dropped and take the
            # closing-fill path below.
            self.stop_client_order_ids.pop(event.client_order_id, None)
            self.stop_orders.pop(inst_id, None)
            stopped = self.states.pop(inst_id, None)
            if stopped is not None:
                self._close_journal_trade(inst_id, stopped, ExitReason.STOP_LOSS)
                self._record_exit_fill(event)
            key = str(ExitReason.STOP_LOSS)
            self.exit_counts[key] = self.exit_counts.get(key, 0) + 1
            self.log.info(f"EXIT {ExitReason.STOP_LOSS} {inst_id} (venue stop filled)")
            return

        if event.client_order_id in self._close_orders:
            self._record_close_fill(event)  # our own closing order (C-01)
            return
        state = self.states.get(inst_id)
        entry_id = state.meta.get("entry_order_id") if state is not None else None
        if state is None or (
            entry_id is not None and not _same_order(event.client_order_id, entry_id)
        ):
            self._record_exit_fill(event)  # a closing fill, not our entry
            return
        if float(event.last_qty) <= 0 or float(event.last_px) <= 0:
            # R18: validate venue values before using them. A zero-quantity fill
            # was accepted by a real venue and caused 537 div-by-zero crashes.
            self.log.error(f"{inst_id} implausible fill qty={event.last_qty} px={event.last_px}")
            return

        if self._stop_attached_at_entry() and "gap_ms" not in state.meta:
            # The stop was live before this fill: the window is a measured zero.
            state.meta["gap_ms"] = 0
            trade_id_gap = state.meta.get("trade_id")
            if self.journal and trade_id_gap:
                try:
                    self.journal.update_trade(trade_id_gap, protect_gap_ms=0)
                except Exception as e:
                    self.log.error(f"journal protect_gap write failed: {e}")
        trade_id = state.meta.get("trade_id")
        self._record_entry_fill(state, event, trade_id)
        if self.journal and trade_id:
            # The read is inside the try too: a raise here used to skip the stop
            # below -- the journal never goes first and never raises (R54, N2-05).
            try:
                if self.journal.trade_state(trade_id) == "submitted":
                    self.journal.transition(trade_id, "open", entry_price=float(event.last_px))
            except Exception as e:
                self.log.error(f"journal open failed for {trade_id}: {e}")

        # Entries can fill in pieces; the stop must always cover everything
        # filled so far, not just the first piece (F030).
        state.meta["filled_qty"] = state.meta.get("filled_qty", 0.0) + float(event.last_qty)
        state.meta.setdefault("first_fill_ts", event.ts_event)
        if (self._stop_closes_position() or self._stop_attached_at_entry()) and (
            inst_id in self.stop_orders
        ):
            # A close-position stop, or an OCO attached at entry, already covers
            # every later fill -- and OKX allows only one per position.
            return
        qty = stop_topup_needed(state.meta["filled_qty"], state.meta.get("covered_qty", 0.0))
        if qty is None:
            return
        old = self.stop_orders.get(inst_id)
        # New stop first, then cancel the old one: never a moment with no stop.
        # Both are reduce-only, so the brief overlap cannot over-close.
        self._submit_stop(inst_id, state, qty)
        if old is not None:
            self.stop_client_order_ids.pop(old.client_order_id, None)
            self._cancel_order_id(old.client_order_id)

    def on_order_rejected(self, event) -> None:
        self._handle_refused(event, "rejected")

    def on_order_denied(self, event) -> None:
        self._handle_refused(event, "denied")

    # OKX allows exactly one close-position stop per position (F036), so this
    # refusal is the venue saying the position ALREADY has its stop.
    DUPLICATE_STOP_SIGNS = ("only place 1 tp/sl", "only place one tp/sl")

    # How far the venue's trigger may sit from the one this trade asked for and
    # still be the same stop. OKX rounds the trigger to the instrument tick,
    # which is far inside this; a stop left by an earlier position is percent
    # away (F065: 115.597 against a wanted 119.374, 3.2%).
    STALE_STOP_TOLERANCE_PCT = 0.1

    def _stop_price_matches(self, venue_price: float, wanted: float) -> bool:
        if not wanted:
            return False
        return abs(venue_price - wanted) / abs(wanted) * 100.0 <= self.STALE_STOP_TOLERANCE_PCT

    def _replace_stale_stop(self, inst_id, state, venue_price: float, wanted: float) -> bool:
        """A close-position stop is live on this instrument, but it is not ours.

        It belongs to an earlier position on the same instrument whose stop
        outlived it -- see `_reap_orphan_stops` for how one survives. The
        position is *guarded*, so closing it immediately is the wrong first move,
        but it is guarded at the wrong distance: the F065 case ran 1.8x its risk
        budget with every report still showing the intended stop.

        So cancel the impostor and place the real stop. If we cannot (no release
        hook, or the venue refuses the cancel) then we cannot make this position
        match its risk budget, and R15 decides: close it.

        Returns True when the refusal has been dealt with here.
        """
        risk_note = (
            f"{inst_id} STALE VENUE STOP: the venue holds a stop at {venue_price:.6g} but this "
            f"position needs {wanted:.6g} -- it belongs to an earlier position on this instrument"
        )
        self.unprotected_events += 1
        release = getattr(self, "venue_stop_hook", None)
        if release is None:
            self.log.error(f"{risk_note}; no way to cancel it -- CLOSING")
            self._jevent(
                "stale_stop_unfixable",
                severity="critical",
                instrument=str(inst_id),
                reason=risk_note,
            )
            return False
        try:
            release(str(inst_id))
        except Exception as e:
            self.log.error(f"{risk_note}; cancelling it failed ({e}) -- CLOSING")
            self._jevent(
                "stale_stop_unfixable",
                severity="critical",
                instrument=str(inst_id),
                reason=f"{risk_note}; cancel failed: {e}",
            )
            return False
        self.log.error(f"{risk_note}; cancelled it and re-placing at {wanted:.6g}")
        self._jevent(
            "stale_stop_replaced",
            severity="critical",
            instrument=str(inst_id),
            reason=risk_note,
        )
        qty = state.meta.get("covered_qty") or abs(getattr(state, "base_units", 0.0))
        self._submit_stop(inst_id, state, qty)
        return True

    def _already_protected(self, inst_id, reason: str) -> bool:
        """Is this stop refusal actually "you already have one"?

        A refused stop normally means the position is naked, and a naked position
        gets closed (R15). But measured on the box during the R44 drill: a
        re-attach raced the venue and OKX answered "You can only place 1 TP/SL
        order to close an entire position" -- which means the position is
        protected. Closing it there exits a healthy trade for no reason, and on
        live that is a mystery exit nobody can explain afterwards.

        So for that refusal only, ask the venue read-only whether our stop is
        really there (R11 -- measure, do not infer). Anything we cannot verify
        keeps the conservative behaviour and the position is closed.
        """
        if not any(sign in (reason or "").lower() for sign in self.DUPLICATE_STOP_SIGNS):
            return False
        peek = getattr(self, "venue_stop_peek_hook", None)
        if peek is None:
            return False
        try:
            price = peek(str(inst_id))
        except Exception as e:
            self.log.error(f"{inst_id} stop refused as duplicate but the venue would not say: {e}")
            return False
        if price is None:
            return False
        state = self.states.get(inst_id)
        # A live stop is not this trade's stop (F065). Before F065 any trigger
        # price counted, so a stop left behind by an earlier position on the
        # same instrument read as protection and the new trade silently ran at
        # whatever risk that old trigger implied.
        wanted = getattr(state, "stop_price", None) if state is not None else None
        if wanted is not None and not self._stop_price_matches(float(price), float(wanted)):
            return self._replace_stale_stop(inst_id, state, float(price), float(wanted))
        if state is not None:
            # Remember what is actually guarding the position; the watchdog checks
            # venue positions against live stops, not this, but an operator
            # reading the journal should see which price is in force.
            state.meta["venue_stop_price"] = float(price)
        self.log.warning(
            f"{inst_id} stop refused as a duplicate, and the venue confirms one at {price} "
            f"-- the position is protected, not naked; not closing"
        )
        self._jevent(
            "stop_duplicate",
            severity="warning",
            instrument=str(inst_id),
            reason=f"{reason} | venue stop live at {price}",
        )
        return True

    def _handle_refused(self, event, how: str) -> None:
        """An order the venue (or nautilus's risk engine) refused (F035).

        Entry refused  -> forget the intended position; otherwise the phantom
                          state blocks the instrument forever.
        Stop refused   -> the position is naked; flatten it now (R15).
        """
        oid = event.client_order_id
        reason = getattr(event, "reason", "")
        close_inst = self._close_orders.pop(oid, None)
        if close_inst is not None:
            state = self.states.get(close_inst)
            if state is not None:
                self._close_refused(close_inst, state, how, reason)
            return
        stop_inst = self.stop_client_order_ids.pop(oid, None)
        if stop_inst is not None:
            self.stop_orders.pop(stop_inst, None)
            self.stops_refused += 1
            if self._already_protected(stop_inst, reason):
                return
            self.unprotected_events += 1
            self.log.error(f"{stop_inst} protective stop {how}: {reason} -- CLOSING")
            # Safety action first, journal second (O-04).
            refused = self.states.get(stop_inst)
            if refused is not None:
                # The position is NOT forgotten before the venue says flat
                # (R49/C-01): the old code popped the state and fired an
                # untracked close, so a rejected flatten left a naked, invisible
                # position with no retry (B-02).
                self._exit(stop_inst, ExitReason.STOP_REFUSED)
            else:
                self._flatten(stop_inst)
            self._jevent(
                "stop_refused", severity="critical", instrument=str(stop_inst), reason=reason
            )
            return

        for inst_id, state in list(self.states.items()):
            if not _same_order(state.meta.get("entry_order_id"), oid):
                continue
            self.states.pop(inst_id, None)
            self.entries_refused = getattr(self, "entries_refused", 0) + 1
            self.log.warning(f"{inst_id} entry {how}: {reason}")
            trade_id = state.meta.get("trade_id")
            if self.journal and trade_id:
                try:
                    if self.journal.trade_state(trade_id) == "submitted":
                        self.journal.transition(trade_id, "rejected")
                    self.journal.event(
                        "entry_refused", severity="warning", instrument=str(inst_id), reason=reason
                    )
                except Exception as e:
                    self.log.error(f"journal reject failed for {trade_id}: {e}")
            return

    def _has_position(self, inst_id) -> bool:
        return bool(self.cache.positions_open(instrument_id=inst_id))

    def on_order_canceled(self, event) -> None:
        """A tracked stop cancelled by someone else: re-attach now (drill B).

        Our own cancels drop tracking first, so they never reach this branch.
        """
        inst_id = self.stop_client_order_ids.pop(event.client_order_id, None)
        if inst_id is None:
            self._entry_canceled(event)
            return
        self.stop_orders.pop(inst_id, None)
        state = self.states.get(inst_id)
        if state is None or not self._has_position(inst_id):
            return
        if state.meta.get("closing"):
            # The venue cancels a linked stop when the position closes; that is
            # our own exit completing, not someone removing protection.
            self.log.info(f"{inst_id} stop cancelled as the position closes")
            return
        self.unprotected_events += 1
        self.log.error(f"{inst_id} protective stop cancelled externally -- re-attaching")
        self._submit_stop(inst_id, state, state.meta.get("filled_qty", 0.0))
        self._jevent("stop_cancelled_externally", severity="critical", instrument=str(inst_id))

    def _entry_canceled(self, event) -> None:
        """An entry cancelled or expired before (or during) its fills (H-03).

        With no fill the intended position never existed: drop it, or it blocks
        the instrument forever. With a partial fill keep the REAL size and make
        sure the stop covers exactly that.
        """
        for inst_id, state in list(self.states.items()):
            if not _same_order(state.meta.get("entry_order_id"), event.client_order_id):
                continue
            filled = float(state.meta.get("filled_qty") or 0.0)
            trade_id = state.meta.get("trade_id")
            if filled <= 0:
                self.states.pop(inst_id, None)
                self.log.warning(f"{inst_id} entry cancelled before any fill -- state dropped")
                if self.journal and trade_id:
                    try:
                        if self.journal.trade_state(trade_id) == "submitted":
                            self.journal.transition(trade_id, "cancelled")
                        self.journal.event(
                            "entry_cancelled", severity="warning", instrument=str(inst_id)
                        )
                    except Exception as e:
                        self.log.error(f"journal cancel failed for {trade_id}: {e}")
                return
            self.log.warning(
                f"{inst_id} entry cancelled after a partial fill: keeping {filled} "
                f"(planned {state.meta.get('qty')})"
            )
            if self.journal and trade_id:
                try:
                    self.journal.update_trade(trade_id, qty=filled)
                except Exception as e:
                    self.log.error(f"journal qty update failed for {trade_id}: {e}")
            covered = float(state.meta.get("covered_qty") or 0.0)
            if self._has_position(inst_id) and covered < filled:
                self._submit_stop(inst_id, state, filled)
            return

    def _venue_writes_allowed(self, what: str = "") -> bool:
        """Observe mode never orders, never adopts, NEVER CANCELS (R48).

        Adoption and the entry path were gated; the repair paths were not, and
        the watchdog's stale-stop sweep cancels orders. In observe mode nothing
        is tracked while `external_order_claims` still routes the account's real
        stops to us, so every one of them looked untracked (L-08).
        """
        if self.config.trading_enabled:
            return True
        if what:
            self.log.info(f"OBSERVE: would {what}")
        return False

    def _venue_agrees_position(self, inst_id, cached_qty: float) -> bool:
        """Does the VENUE hold what the cache says, on the same side? (F069)

        Closing is not a safe default. A close order is a market order in the
        opposite direction, so if the position is not really there the "close"
        OPENS one -- real money, wrong direction, no stop on it. That happened
        twice on the live account: the cache held a phantom SHORT (nautilus
        reconciles fills over a limited lookback and had seen the closing sells
        without the opening buys), so `flatten` bought, and the buy opened a long.

        It is also self-sustaining, which is what makes it urgent rather than
        merely wrong: every manual close teaches the cache a new phantom short,
        and the node answers by buying the position straight back. The owner
        closed LINK at 03:57 and the node re-opened it at 04:00.

        So the venue decides. Same side only -- a disagreement about *direction*
        is the dangerous one, and one we can check cheaply. If the venue cannot be
        read we do not send: not knowing is not "it is there" (R70), and unlike a
        journal write this path spends money. The watchdog comes back in 20 min.

        No hook (a backtest) means no venue to ask, and the cache is the truth
        there by construction, so the guard stands aside (R3).
        """
        hook = getattr(self, "venue_position_hook", None)
        if hook is None:
            return True
        try:
            held = hook() or {}
        except Exception as e:
            reason = f"could not read the venue before closing {inst_id} ({e})"
            self.log.error(f"REFUSING TO CLOSE {inst_id}: {reason} -- an unverified close can OPEN a position")  # fmt: skip
            self._jevent("phantom_flatten_refused", severity="critical", instrument=str(inst_id), reason=reason)  # fmt: skip
            return False
        key = str(inst_id).removesuffix(".OKX")
        venue_qty = float(held.get(key, held.get(str(inst_id), 0.0)) or 0.0)
        same_side = bool(venue_qty) and (venue_qty > 0) == (cached_qty > 0)
        # Size, not only direction. Closing MORE than the venue holds sells what
        # is there and then keeps going: cache 1.2 against a real 0.6 leaves a
        # 0.6 short. Closing less is safe -- a real remainder stays, and the
        # watchdog repairs it. Only overshoot creates a position.
        overshoot = abs(cached_qty) > abs(venue_qty) * (1 + 1e-9)
        if same_side and not overshoot:
            return True
        reason = (
            f"the cache holds {cached_qty} but the venue holds {venue_qty or 'nothing'}; "
            f"closing that would open a position rather than close one"
        )
        self.log.error(f"REFUSING TO CLOSE {inst_id}: {reason}")
        self._jevent(
            "phantom_flatten_refused",
            severity="critical",
            instrument=str(inst_id),
            reason=reason,
        )
        return False

    def _flatten(self, inst_id) -> None:
        if not self._venue_writes_allowed(f"flatten {inst_id}"):
            return
        for oid in [o for o, i in self.stop_client_order_ids.items() if i == inst_id]:
            self.stop_client_order_ids.pop(oid, None)  # our cancel, not an external one
        self.stop_orders.pop(inst_id, None)
        self.cancel_all_orders(inst_id)
        for position in self.cache.positions_open(instrument_id=inst_id):
            if self._venue_agrees_position(inst_id, float(position.signed_qty)):
                self.close_position(position)

    def on_order_accepted(self, event) -> None:
        """Record F007's unprotected window: entry fill -> stop live at venue."""
        inst_id = self.stop_client_order_ids.get(event.client_order_id)
        if inst_id is None or not self.journal:
            return
        state = self.states.get(inst_id)
        if state is None or "gap_ms" in state.meta or "first_fill_ts" not in state.meta:
            return
        gap_ms = int((event.ts_event - state.meta["first_fill_ts"]) // 1_000_000)
        state.meta["gap_ms"] = gap_ms
        trade_id = state.meta.get("trade_id")
        if trade_id:
            try:
                self.journal.update_trade(trade_id, protect_gap_ms=gap_ms)
            except Exception as e:
                self.log.error(f"journal protect_gap write failed for {trade_id}: {e}")
        self.log.info(f"{inst_id} protected {gap_ms} ms after entry fill")

    def _stop_attached_at_entry(self) -> bool:
        return bool(self.config.venue_attaches_stop)

    def _submit_entry(self, inst_id, direction: Direction, qty: float, stop_price: float) -> object:
        """Send the entry. Returns its client order id.

        With `venue_attaches_stop` the stop travels WITH the entry as an OCO
        child, so there is no unprotected window (G7). nautilus's bracket
        factory always builds a take-profit child too: it is parked far away
        because S003 has no profit target (ADR-0008).
        """
        instrument = self.instruments[inst_id]
        side = OrderSide.BUY if direction is Direction.LONG else OrderSide.SELL
        quantity = instrument.make_qty(qty)
        if not self._stop_attached_at_entry():
            order = self.order_factory.market(
                instrument_id=inst_id,
                order_side=side,
                quantity=quantity,
                time_in_force=TimeInForce.GTC,
            )
            self.submit_order(order)
            return order.client_order_id

        far = stop_price * (1.5 if direction is Direction.LONG else 0.5)
        bracket = self.order_factory.bracket(
            instrument_id=inst_id,
            order_side=side,
            quantity=quantity,
            entry_order_type=OrderType.MARKET,
            time_in_force=TimeInForce.GTC,
            sl_trigger_price=instrument.make_price(stop_price),
            sl_trigger_type=TriggerType.LAST_PRICE,
            tp_order_type=OrderType.MARKET_IF_TOUCHED,
            tp_trigger_price=instrument.make_price(far),
            tp_trigger_type=TriggerType.LAST_PRICE,
        )
        entry, stop = bracket.orders[0], bracket.orders[1]
        self.submit_order_list(bracket)
        self.stop_orders[inst_id] = stop
        self.stop_client_order_ids[stop.client_order_id] = inst_id
        self.log.info(f"{inst_id} stop attached to the entry @ {stop_price:.6g}")
        return entry.client_order_id

    def _stop_closes_position(self) -> bool:
        return self.config.venue_stop_closes_position

    def _submit_stop(self, inst_id, state: PositionState, qty: float) -> None:
        if not self._venue_writes_allowed(f"attach a stop on {inst_id}"):
            return
        instrument = self.instruments[inst_id]
        side = OrderSide.SELL if state.direction is Direction.LONG else OrderSide.BUY
        try:
            stop = self.order_factory.stop_market(
                instrument_id=inst_id,
                order_side=side,
                quantity=instrument.make_qty(qty),
                trigger_price=instrument.make_price(state.stop_price),
                trigger_type=TriggerType.LAST_PRICE,
                reduce_only=True,
                time_in_force=TimeInForce.GTC,
            )
        except Exception as e:  # never leave a position naked silently
            self.unprotected_events += 1
            self.log.error(f"{inst_id} PROTECTIVE STOP FAILED: {e}")
            return
        params = {"close_fraction": "1"} if self._stop_closes_position() else None
        self.submit_order(stop, params=params)
        self.stop_orders[inst_id] = stop
        self.stop_client_order_ids[stop.client_order_id] = inst_id
        state.meta["covered_qty"] = qty
        self.log.info(f"{inst_id} protective stop qty={qty} @ {state.stop_price:.6g}")

    def _cancel_order_id(self, client_order_id) -> None:
        if not self._venue_writes_allowed(f"cancel {client_order_id}"):
            return
        cached = self.cache.order(client_order_id)
        if cached is not None and not cached.is_closed:
            self.cancel_order(cached)

    def _cancel_protective_stop(self, inst_id) -> None:
        order = self.stop_orders.pop(inst_id, None)
        if order is None:
            return
        self.stop_client_order_ids.pop(order.client_order_id, None)
        self._cancel_order_id(order.client_order_id)

    def on_position_closed(self, event: PositionClosed) -> None:
        inst_id = event.instrument_id
        state = self.states.get(inst_id)
        # A state whose entry has not filled belongs to a NEW position; the
        # close event is about the old one (review H1).
        if state is not None and state.meta.get("filled_qty", 0.0) > 0:
            if state.meta.get("closing"):
                self._close_confirmed(inst_id, state)
            elif getattr(self, "venue_position_hook", None) is not None and (
                self._venue_still_holds(inst_id, state)
            ):
                # A cache closure is not a venue closure (R72). Our own fill can
                # close a phantom in the cache under NETTING (P1); the venue still
                # holds our position, so the state and its stop stay.
                pass
            else:
                # Closed by something that is not us -- a hand-flatten, another
                # tool, a liquidation. The venue is the truth (R27), so the
                # journal row closes here or it stays open forever (F047).
                self.states.pop(inst_id, None)
                self._close_journal_trade(inst_id, state, "closed_externally")
                self._cancel_protective_stop(inst_id)
        pending = self._pending_entry.pop(inst_id, None)
        if pending is not None:
            self._enter(inst_id, *pending)

    def _venue_still_holds(self, inst_id, state: PositionState) -> bool:
        """Before forgetting a position we did not close: is it really gone? (R72)

        True keeps the state and its venue stop: the venue holds our side, or
        could not be read (R70 -- a stop on nothing is harmless, cancelling the
        only stop on something is not, R62). No hook (a backtest): the cache is
        the truth there by construction (R3), so False.
        """
        hook = getattr(self, "venue_position_hook", None)
        if hook is None:
            return False
        try:
            held = hook() or {}
            key = str(inst_id).removesuffix(".OKX")
            venue_qty = float(held.get(key, held.get(str(inst_id), 0.0)) or 0.0)
        except Exception as e:
            reason = f"the cache closed {inst_id} but the venue could not be read ({e})"
        else:
            if not venue_qty or (venue_qty > 0) != (state.direction is Direction.LONG):
                return False
            reason = (
                f"the cache closed {inst_id} but the venue still holds {venue_qty}; "
                f"keeping the state and its stop"
            )
        self.log.error(f"POSITION KEPT: {reason}")
        self._jevent(
            "cache_closed_venue_holds", severity="critical", instrument=str(inst_id), reason=reason
        )
        return True

    def _stop_is_live(self, order: object) -> bool:
        cached = self.cache.order(order.client_order_id)
        return cached is not None and not cached.is_closed

    def on_order_cancel_rejected(self, event) -> None:
        cached = self.cache.order(event.client_order_id)
        if cached is not None and cached.is_closed:
            # OKX sends a duplicate reject after a cancel already succeeded
            # (rehearsal #7): nothing is live, so this is not an alarm (F041).
            self.log.info(f"{event.client_order_id} cancel reject after close -- ignored")
            return
        # The order stays live; the watchdog's stale-stop sweep retries it.
        self.log.error(f"{event.instrument_id} cancel rejected for {event.client_order_id}")
        if self.journal:
            self.journal.event(
                "cancel_rejected",
                severity="critical",
                instrument=str(event.instrument_id),
                order=str(event.client_order_id),
            )

    def _sweep_stale_stops(self) -> None:
        if not self._venue_writes_allowed("sweep untracked venue stops"):
            return
        for inst_id in self.instruments:
            open_stops = {
                o.client_order_id: o
                for o in self.cache.orders_open(instrument_id=inst_id, strategy_id=self.id)
                if o.order_type.name in ("STOP_MARKET", "STOP_LIMIT")
            }
            for oid in stale_stops(open_stops, self.stop_client_order_ids):
                self.log.error(f"WATCHDOG: cancelling untracked stop {oid} on {inst_id}")
                if self.journal:
                    self.journal.event("stale_stop", severity="warning", instrument=str(inst_id))
                self.cancel_order(open_stops[oid])

    def _check_venue_inventory(self) -> None:
        """Does the venue hold anything this node knows nothing about? (F067)

        nautilus reconciles positions on a best-effort basis and says so when it
        gives up:

            Position discrepancy for LINK-USDT-SWAP.OKX unresolved after 3
            attempts (cached_qty=0, venue_qty=5.69); no further reconciliation
            attempts will be made

        A position that never reaches the cache is invisible to every safety net
        we own, because all of them are keyed on our own view: it is not in the
        snapshot, so the dashboard cannot draw it; `positions_unprotected` never
        counts it; the strategy has no state, so it has no exit and no trailing
        stop. It is entirely real all the same.

        R53 already insists the unprotected metric be defined over venue
        positions rather than internal state, "the cases it exists to catch are
        exactly the ones with no state". The inventory itself needs the same
        treatment.

        This alarms, it does not refuse: the node cannot create the cache entry
        nautilus declined to make, and taking every other position down to
        protest one is worse than shouting about it every pass.
        """
        hook = getattr(self, "venue_position_hook", None)
        if hook is None:
            return
        try:
            held = hook()
        except Exception as e:
            # Not knowing is not "nothing" (R70) -- say so rather than pass.
            self.log.warning(f"WATCHDOG: could not read the venue's own position list: {e}")
            self._jevent("venue_inventory_unreadable", severity="warning", reason=str(e))
            return
        ours = {str(i).removesuffix(".OKX"): i for i in self.instruments}
        # The other direction: the cache holds something the venue does not.
        # Nothing real is at risk, but every count derived from the cache is
        # wrong -- including `positions_unprotected`, which reported two naked
        # positions on a flat account and failed a live deploy's final gate
        # (F069 tail). Recorded here so that metric can stay honest (R53).
        # Alert on change (R75): a phantom or a protected unmanaged position that
        # has not moved is ONE journal event, not one per 20-min pass -- the BNB
        # phantom of 2026-10-07 wrote 72 criticals a day and held live's health
        # red for days over one fact. The log still records every pass. A NAKED
        # unmanaged position is journalled every pass on purpose: money is at
        # risk, and the alerter's own dedup turns that into 6-hourly reminders.
        before = getattr(self, "_inventory_reported", set())
        reported: set = set()
        phantoms = set()
        for inst_id in self.instruments:
            key = str(inst_id).removesuffix(".OKX")
            if not abs(float(self._position_qty(inst_id) or 0.0)):
                continue
            if abs(float((held or {}).get(key, (held or {}).get(str(inst_id), 0.0)) or 0.0)):
                continue
            phantoms.add(str(inst_id))
            self.log.error(
                f"WATCHDOG: this node believes it holds {inst_id} but the venue has no such "
                f"position. Nothing real is open; the cache is wrong and every count from it is."
            )
            reported.add(("phantom", str(inst_id)))
            if ("phantom", str(inst_id)) not in before:
                self._jevent(
                    "phantom_cache_position",
                    severity="critical",
                    instrument=str(inst_id),
                    reason="the node's cache holds a position the venue does not have",
                )
        self._phantom_instruments = phantoms
        unmanaged = 0
        for inst, qty in (held or {}).items():
            key = str(inst).removesuffix(".OKX")
            if key not in ours or not float(qty or 0.0):
                continue
            if abs(self._position_qty(ours[key])) > 0:
                continue
            unmanaged += 1
            # Unmanaged is not one condition, it is two, and they want opposite
            # responses. Naked: money is at risk now. Protected: the loss is
            # capped at the number we chose, and what is actually lost is the
            # UPSIDE -- the Donchian exit and the trailing stop cannot run, and
            # on S003 both live winners so far exited on the trailing stop.
            # Sending them at the same severity is how a channel earns a mute
            # (R75), and this project has already paid that bill once.
            peek = getattr(self, "venue_stop_peek_hook", None)
            stop = None
            if peek is not None:
                try:
                    stop = peek(str(ours[key]))
                except Exception as e:
                    # Not knowing whether it is protected is not knowing it is
                    # safe (R70): fall through to the naked branch.
                    self.log.warning(f"could not check {key} for a venue stop: {e}")
            if stop is None:
                self.log.error(
                    f"WATCHDOG: the venue holds {qty} {key} that this node does not manage "
                    f"and that has NO venue stop -- nothing caps this loss. Reconciliation "
                    f"never took it."
                )
                self._jevent(
                    "unmanaged_venue_position",
                    severity="critical",
                    instrument=str(ours[key]),
                    reason=f"venue holds {qty} with no stop and the node has no position for it",
                )
            else:
                self.log.warning(
                    f"WATCHDOG: the venue holds {qty} {key} that this node does not manage. "
                    f"Its venue stop at {stop} still caps the loss, but no Donchian exit and "
                    f"no trailing stop can run on it."
                )
                reported.add(("unmanaged", str(ours[key])))
                if ("unmanaged", str(ours[key])) in before:
                    continue
                self._jevent(
                    "unmanaged_venue_position",
                    severity="warning",
                    instrument=str(ours[key]),
                    reason=(
                        f"venue holds {qty}, protected by a venue stop at {stop}, but the node "
                        f"has no state for it: the loss is capped, the exits are not running"
                    ),
                )
        self.unmanaged_venue_positions = unmanaged
        self._inventory_reported = reported

    def _protection_watchdog(self, _event=None) -> None:
        """Re-verify every open position still has a live venue stop (R15).

        Also cancels open stops nobody tracks (`stale_stops`): a failed cancel
        leaves one live, and on OKX it blocks every new stop on that instrument.

        The guards run here too, but in their own try: they read the journal and
        the venue, and a raise used to abort the whole pass -- no stale-stop
        sweep, no close retries, no re-attach, no orphan repair (N2-07).
        """
        try:
            self._risk_check()
        except Exception as e:
            self.log.error(f"WATCHDOG: risk check failed: {e}")
        self._sweep_stale_stops()
        self._check_venue_inventory()
        # Retry an exit whose close never completed; the stop is still in place.
        for inst_id, state in list(self.states.items()):
            if not state.meta.get("closing") or state.meta.get("close_halted"):
                continue
            # With a venue to ask, an empty cache is not "done" (R72): the retry
            # reads the venue, which either closes what is left or confirms flat.
            maybe_open = self._position_qty(inst_id) > 0 or self.venue_position_hook is not None
            if maybe_open and int(state.meta.get("close_attempts", 0)) < 3:
                self.log.error(f"WATCHDOG: exit of {inst_id} unfinished -- resubmitting close")
                self._submit_close(inst_id, state)
        now = self.clock.timestamp_ns()
        for inst_id, since in list(self._awaiting_since.items()):
            bt = BarType.from_str(f"{inst_id}-{self.config.bar_spec}")
            if now - since > 2 * bt.spec.timedelta.total_seconds() * 1_000_000_000:
                # R20: a history request that never answers must not block forever.
                self.log.error(f"WATCHDOG: history for {inst_id} never arrived -- proceeding")
                self._history_complete(inst_id)
        for inst_id in self.instruments:
            interval = int(
                BarType.from_str(f"{inst_id}-{self.config.bar_spec}").spec.timedelta.total_seconds()
                * 1_000_000_000
            )
            if data_stalled(
                self._last_live_bar_ns.get(inst_id), now, interval_ns=interval, grace_ns=60 * 10**9
            ):
                self.log.error(f"WATCHDOG: no live bars for {inst_id} -- data feed stalled")
                self._jevent("data_stall", severity="critical", instrument=str(inst_id))
        self.log.info(
            f"watchdog: live_bars={self._live_bars} open={len(self.states)} "
            f"unprotected={self.positions_unprotected}"
        )
        held = [i for i in self.states if self.cache.positions_open(instrument_id=i)]
        for inst_id in needs_protection(held, self.stop_orders, self._stop_is_live):
            self.unprotected_events += 1
            self.log.error(f"WATCHDOG: {inst_id} has no live protective stop -- re-attaching")
            self.stop_orders.pop(inst_id, None)
            for pos in self.cache.positions_open(instrument_id=inst_id):
                self._reattach_stop(inst_id, pos)
        self._repair_orphan_positions()

    def _repair_orphan_positions(self) -> None:
        """Close a VENUE position we hold no state for (N2-04).

        Every exit in this strategy is keyed on `states`: the Donchian exit, the
        reversal, the trailing stop, the stop attachment. A position the venue
        reports and we cannot explain -- a fill that landed after its entry was
        cancelled or rejected, a KILL that raced an unfilled entry -- therefore
        has no way out at all. It was counted and alerted (B-03) and then left
        exactly where it was. Being told is not repairing (R15).

        Closing is the repair, not adopting: adopting would mean inventing an
        entry price and a stop for a position we cannot account for, while a
        confirmed close is deterministic (R49) and the account is ours alone
        (R51). An instrument with orders still open is skipped -- a fill can
        arrive while the entry is settling, and that is not an orphan.
        """
        for inst_id in self.instruments:
            if inst_id in self.states:
                continue
            if not self.cache.positions_open(instrument_id=inst_id):
                continue
            if self.cache.orders_open(instrument_id=inst_id):
                continue
            if str(inst_id) in getattr(self, "_phantom_instruments", set()):
                # Not an orphan: the venue says there is nothing here at all, so
                # there is nothing to exit and nothing to protect. Trying anyway
                # had `_flatten` refuse it every pass and announced two criticals
                # each time -- 854 of the 1,700 alerts in 72 hours (F070). The
                # phantom has its own alarm; this one would be a lie.
                continue
            self.orphans_closed += 1
            self.log.error(
                f"WATCHDOG: {inst_id} is open at the venue with no state -- "
                f"nothing here can ever exit it; closing"
            )
            self._flatten(inst_id)
            self._jevent(
                "orphan_position",
                severity="critical",
                instrument=str(inst_id),
                reason="venue position with no strategy state -- close attempted by the watchdog (N2-04)",
            )
            self._jevent("position_unprotected", severity="critical", instrument=str(inst_id))

    def _reattach_stop(self, inst_id, position) -> None:
        state = self.states.get(inst_id)
        if state is None:
            return
        self._submit_stop(inst_id, state, float(position.quantity))

    def positions_unprotected_count(self) -> int:
        """Positions with no LIVE venue stop. Must always be 0 (R15).

        Two blind spots the first version had (B-03/O-03, review 2026-09-18):
        it tested dict membership rather than whether the stop is still alive at
        the venue, and it iterated `self.states`, so a venue position the
        strategy has no state for -- the exact case the metric exists to catch --
        counted as protected.
        """
        naked = 0
        seen = set()
        for inst_id in self.states:
            seen.add(inst_id)
            stop = self.stop_orders.get(inst_id)
            if stop is None or not self._stop_is_live(stop):
                naked += 1
        phantoms = getattr(self, "_phantom_instruments", set())
        for inst_id in getattr(self, "instrument_ids", ()):
            if inst_id in seen:
                continue
            if str(inst_id) in phantoms:
                # The venue says this is not there (checked by the watchdog
                # against the venue itself, not against our own state). A
                # position that does not exist has no stop, and demanding one is
                # how a flat account reported two naked positions (R53).
                continue
            try:
                if self.cache.positions_open(instrument_id=inst_id):
                    naked += 1
            except Exception:  # cache not available (unit tests)
                continue
        return naked

    @property
    def positions_unprotected(self) -> int:
        return self.positions_unprotected_count()
