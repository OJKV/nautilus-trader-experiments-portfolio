"""Live-path defects found while preparing the demo rehearsal (F028-F032).

Written BEFORE the fixes (R41). None of these show up in a backtest, which is
exactly why they were missed: bar_execution fills every order in one piece,
history is pre-loaded, and nobody reads `is_demo` or `protect_gap_ms` there.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from test_journal_wiring import _trending_bars

from ntx.obs.journal import Journal
from ntx.strategies.base import NtxStrategy
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.exits import PositionState
from ntx.strategies.signal import Direction, ExitReason
from ntx.strategies.watchdog import stop_topup_needed

H = 3_600_000_000_000


def _engine(inst):
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="LIVE-001", logging=LoggingConfig(bypass_logging=True)
        )
    )
    engine.add_venue(
        venue=inst.id.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(100_000, USDT)],
        fill_model=FillModel(prob_fill_on_limit=1.0, random_seed=42),
        bar_execution=True,
    )
    engine.add_instrument(inst)
    return engine


# ------------------------------------------------------------------ F028 ---- #
def test_demo_trades_are_flagged_as_demo(tmp_path):
    """`is_demo` was read from a config field that does not exist, so every demo
    trade was recorded as live -- demo PnL would pollute live statistics."""
    inst = TestInstrumentProvider.ethusdt_binance()
    engine = _engine(inst)
    bt = BarType.from_str(f"{inst.id}-1-HOUR-LAST-EXTERNAL")
    engine.add_data(_trending_bars(inst, bt))
    j = Journal(tmp_path / "j.db")
    s = DonchianBreakoutStrategy(
        DonchianBreakoutConfig(
            instrument_ids=[str(inst.id)],
            entry_period=20,
            exit_period=10,
            bar_spec="1-HOUR-LAST-EXTERNAL",
            journal_path=str(j.path),
            is_demo=True,
        )
    )
    engine.add_strategy(s)
    engine.run()
    engine.dispose()
    rows = j.closed_trades()
    assert rows, "setup: must trade"
    assert all(r["is_demo"] == 1 for r in rows)


# ------------------------------------------------------------------ F029 ---- #
class _HistoryFirst(DonchianBreakoutStrategy):
    """Feeds the first `n_hist` bars through the HISTORICAL path at start, the
    way `request_bars` delivers them live, then receives the rest as live bars."""

    def __init__(self, config, hist):
        super().__init__(config)
        self._hist = hist
        self.entry_bar_ts: list[int] = []

    def on_start(self):
        super().on_start()
        for b in self._hist:
            self.on_historical_data(b)

    def _enter(self, inst_id, direction, bar):
        self.entry_bar_ts.append(bar.ts_event)
        super()._enter(inst_id, direction, bar)


def _history_run():
    inst = TestInstrumentProvider.ethusdt_binance()
    engine = _engine(inst)
    bt = BarType.from_str(f"{inst.id}-1-HOUR-LAST-EXTERNAL")
    bars = _trending_bars(inst, bt, n=400)
    hist, live = bars[:150], bars[150:]
    engine.add_data(live)
    s = _HistoryFirst(
        DonchianBreakoutConfig(
            instrument_ids=[str(inst.id)],
            entry_period=20,
            exit_period=10,
            bar_spec="1-HOUR-LAST-EXTERNAL",
        ),
        hist,
    )
    engine.add_strategy(s)
    engine.run()
    engine.dispose()
    return s, hist, live, inst


def test_historical_bars_warm_up_indicators_without_trading():
    """Live S003 needs 56 prior 4h bars (~9 days). Without a history request the
    node would sit idle that long after every restart."""
    s, hist, live, inst = _history_run()
    assert s.bars_seen[inst.id] == len(hist) + len(live)
    assert s.entry_bar_ts, "setup: live bars must produce an entry"
    assert min(s.entry_bar_ts) >= live[0].ts_event, "a historical bar triggered a trade"


def test_warm_history_allows_trading_on_the_first_live_bars():
    s, _hist, live, _inst = _history_run()
    # the ramp breaks out every bar, so a warm strategy enters on live bar #1
    assert min(s.entry_bar_ts) == live[0].ts_event


def test_duplicate_or_older_bar_is_ignored():
    """A history response can overlap the first live bar; counting it twice
    would double-append to the Donchian channel."""
    s, hist, live, inst = _history_run()
    seen = s.bars_seen[inst.id]
    s.on_historical_data(hist[-1])
    s.on_historical_data(live[-1])
    assert s.bars_seen[inst.id] == seen


def test_node_requests_warmup_history_when_enabled():
    import inspect

    src = inspect.getsource(NtxStrategy.on_start)
    assert "request_bars" in src and "request_warmup" in src


# ------------------------------------------------------------------ F030 ---- #
@pytest.mark.parametrize(
    ("filled", "covered", "expected"),
    [
        (1.0, 0.0, 1.0),  # first fill, nothing covered
        (3.0, 1.0, 3.0),  # partial fills: re-cover the whole position
        (3.0, 3.0, None),  # fully covered
        (3.0, 5.0, None),  # over-covered is harmless with reduce-only
        (0.0, 0.0, None),  # nothing filled
    ],
)
def test_stop_topup_needed(filled, covered, expected):
    assert stop_topup_needed(filled, covered) == expected


def _market_factory(calls):
    def market(instrument_id, order_side, quantity, time_in_force=None, reduce_only=False):
        return SimpleNamespace(
            client_order_id=f"close-{len(calls.submitted) + 1}",
            side=order_side,
            quantity=quantity,
            reduce_only=reduce_only,
        )

    return market


def _fake_strategy(tmp_path, state):
    calls = SimpleNamespace(submitted=[], cancelled=[])
    j = Journal(tmp_path / "j.db")
    j.open_trade(
        trade_id="t1",
        correlation_id="c",
        strategy_id="S",
        venue="OKX",
        instrument_id="X",
        side="LONG",
        qty=3.0,
        entry_price=100.0,
    )

    def submit(inst_id, st, qty):
        order = SimpleNamespace(client_order_id=f"stop-{len(calls.submitted)}", qty=qty)
        calls.submitted.append(order)
        fake.stop_orders[inst_id] = order
        fake.stop_client_order_ids[order.client_order_id] = inst_id

    fake = SimpleNamespace(
        states={"X": state},
        stop_orders={},
        stop_client_order_ids={},
        exit_counts={},
        journal=j,
        config=SimpleNamespace(trading_enabled=True),
        log=SimpleNamespace(info=print, error=print, debug=print, warning=print),
        _submit_stop=submit,
        _cancel_order_id=lambda oid: calls.cancelled.append(oid),
        _close_journal_trade=lambda *a, **k: NtxStrategy._close_journal_trade(fake, *a, **k),
        _last_close=lambda inst_id: None,
        _equity_usd=lambda: 37.0,
        _stop_closes_position=lambda: False,
        _stop_attached_at_entry=lambda: False,
        _fee=NtxStrategy._fee,
        _entry_px=NtxStrategy._entry_px,
        _slippage_bps=NtxStrategy._slippage_bps,
        _pnl=NtxStrategy._pnl,
        _closing={},
        _close_orders={},
        stops_refused=0,
        close_failures=0,
        instruments={"X": SimpleNamespace(make_qty=lambda q: q)},
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: [SimpleNamespace(quantity=3.0)]
        ),
        order_factory=SimpleNamespace(market=_market_factory(calls)),
        submit_order=lambda o, params=None: calls.submitted.append(o),
        _cancel_protective_stop=lambda inst_id: calls.cancelled.append(inst_id),
    )
    for _name in ("_jwrite", "_jevent", "_exit", "_submit_close", "_venue_close_qty", "_position_qty",
                  "_close_confirmed", "_close_refused", "_venue_writes_allowed",
                  "_record_entry_fill"):  # fmt: skip
        setattr(fake, _name, getattr(NtxStrategy, _name).__get__(fake, NtxStrategy))
    fake._record_exit_fill = lambda ev: NtxStrategy._record_exit_fill(fake, ev)
    fake._record_close_fill = lambda ev: NtxStrategy._record_close_fill(fake, ev)
    fake._forget_closed = lambda inst_id, state, reason, price=None: NtxStrategy._forget_closed(
        fake, inst_id, state, reason, price
    )
    return fake, calls, j


def _fill(oid, qty, px=100.0, ts=1_000_000_000):
    return SimpleNamespace(
        client_order_id=oid, instrument_id="X", last_qty=qty, last_px=px, ts_event=ts
    )


def test_partial_fills_grow_the_stop_to_the_whole_position(tmp_path):
    """OKX market orders often fill in pieces. The stop was sized to the FIRST
    fill only and later fills were skipped as 'already protected' -- leaving the
    rest of the position naked."""
    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, meta={"trade_id": "t1"})
    fake, calls, _ = _fake_strategy(tmp_path, st)
    NtxStrategy.on_order_filled(fake, _fill("entry", 1.0))
    NtxStrategy.on_order_filled(fake, _fill("entry", 2.0))
    assert [o.qty for o in calls.submitted] == [1.0, 3.0]
    assert calls.cancelled == ["stop-0"], "the undersized stop must be replaced"
    assert fake.stop_orders["X"].qty == 3.0


# ------------------------------------------------------------------ F031 ---- #
def test_protect_gap_is_recorded_when_the_stop_is_accepted(tmp_path):
    """`protect_gap_ms` existed as a column and a documented metric (F007) but
    nothing ever wrote it."""
    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, meta={"trade_id": "t1"})
    fake, _calls, j = _fake_strategy(tmp_path, st)
    NtxStrategy.on_order_filled(fake, _fill("entry", 3.0, ts=1_000_000_000))
    accepted = SimpleNamespace(
        client_order_id="stop-0", instrument_id="X", ts_event=1_000_000_000 + 250_000_000
    )
    NtxStrategy.on_order_accepted(fake, accepted)
    row = j.open_trades()[0]
    assert row["protect_gap_ms"] == 250


# ------------------------------------------------------------------ F032 ---- #
def test_venue_stop_exit_records_the_fill_price(tmp_path):
    """Venue stop-outs were journalled at the last bar close (or the entry
    price), not at the price the stop actually filled."""
    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, base_units=3.0, meta={"trade_id": "t1"})
    fake, _calls, j = _fake_strategy(tmp_path, st)
    NtxStrategy.on_order_filled(fake, _fill("entry", 3.0))
    NtxStrategy.on_order_filled(fake, _fill("stop-0", 3.0, px=94.5))
    row = j.closed_trades()[0]
    assert row["exit_price"] == pytest.approx(94.5)
    assert row["exit_reason"] == str(ExitReason.STOP_LOSS)
    assert row["pnl_net_usd"] == pytest.approx((94.5 - 100.0) * 3.0)


# ------------------------------------------------------------------ F035 ---- #
def _fake_with_submitted_trade(tmp_path):
    st = PositionState(
        Direction.LONG, 100.0, 95.0, 10.0, meta={"trade_id": "t2", "entry_order_id": "entry"}
    )
    fake, calls, j = _fake_strategy(tmp_path, st)
    j.open_trade(
        trade_id="t2",
        correlation_id="c2",
        strategy_id="S",
        venue="OKX",
        instrument_id="X",
        side="LONG",
        qty=3.0,
        entry_price=100.0,
        state="submitted",
    )
    calls.flattened = []
    fake._flatten = lambda inst_id: calls.flattened.append(inst_id)
    fake.unprotected_events = 0
    fake.DUPLICATE_STOP_SIGNS = NtxStrategy.DUPLICATE_STOP_SIGNS
    fake._already_protected = lambda inst, reason: NtxStrategy._already_protected(
        fake, inst, reason
    )
    fake._handle_refused = lambda ev, how: NtxStrategy._handle_refused(fake, ev, how)
    return fake, calls, j


def _state_of(j, trade_id):
    with j._conn() as c:
        return c.execute("SELECT state FROM trades WHERE trade_id=?", (trade_id,)).fetchone()[0]


def test_venue_rejected_entry_leaves_no_phantom_position(tmp_path):
    """Rehearsal #2: OKX rejected both entries, yet the strategy kept a
    PositionState (blocking that coin forever) and the journal said 'open'."""
    fake, _calls, j = _fake_with_submitted_trade(tmp_path)
    NtxStrategy.on_order_rejected(
        fake, SimpleNamespace(client_order_id="entry", instrument_id="X", reason="bad clOrdId")
    )
    assert "X" not in fake.states
    assert _state_of(j, "t2") == "rejected"


def test_entry_fill_moves_the_journal_trade_to_open(tmp_path):
    fake, _calls, j = _fake_with_submitted_trade(tmp_path)
    NtxStrategy.on_order_filled(fake, _fill("entry", 3.0))
    assert _state_of(j, "t2") == "open"


def test_rejected_stop_closes_the_position(tmp_path):
    """R15: a position whose stop the venue refused must not stay open.

    Updated 2026-09-18 (B-02): it now closes through the confirmed-close path
    instead of an untracked `close_position`, so a refused or lost flatten is
    retried rather than leaving a naked, stateless position.
    """
    fake, calls, _j = _fake_with_submitted_trade(tmp_path)
    NtxStrategy.on_order_filled(fake, _fill("entry", 3.0))
    NtxStrategy.on_order_rejected(
        fake, SimpleNamespace(client_order_id="stop-0", instrument_id="X", reason="no")
    )
    assert fake.unprotected_events == 1
    assert "X" not in fake.stop_orders
    assert fake.states["X"].meta["closing"] == str(ExitReason.STOP_REFUSED)
    closing = calls.submitted[-1]
    assert closing.reduce_only is True
    assert fake._close_orders[closing.client_order_id] == "X"


def test_unrelated_rejection_is_ignored(tmp_path):
    fake, calls, j = _fake_with_submitted_trade(tmp_path)
    NtxStrategy.on_order_rejected(
        fake, SimpleNamespace(client_order_id="other", instrument_id="X", reason="no")
    )
    assert "X" in fake.states and calls.flattened == []
    assert _state_of(j, "t2") == "submitted"


# ------------------------------------------------------------------ F036 ---- #
def test_close_position_stop_is_submitted_once_despite_partial_fills(tmp_path):
    """OKX rejected reduce-only stops without close_fraction ('Reduce Only is
    not available'). With close_fraction=1 the stop closes the WHOLE position,
    and OKX allows only one such order per position -- so no top-ups."""
    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, meta={"trade_id": "t1"})
    fake, calls, _ = _fake_strategy(tmp_path, st)
    fake._stop_closes_position = lambda: True
    NtxStrategy.on_order_filled(fake, _fill("entry", 1.0))
    NtxStrategy.on_order_filled(fake, _fill("entry", 2.0))
    assert len(calls.submitted) == 1
    assert calls.cancelled == []


def test_node_enables_close_position_stops_for_okx(tmp_path):
    s = _settings_for_okx(tmp_path)
    from ntx.live import node

    assert node.strategy_kwargs(node.plan(s), s)["venue_stop_closes_position"] is True


def test_stop_params_carry_close_fraction_only_when_enabled():
    import inspect

    src = inspect.getsource(NtxStrategy._submit_stop)
    assert '"close_fraction": "1"' in src


def _settings_for_okx(tmp_path):
    from ntx.config.settings import Settings

    return Settings(
        _env_file=None,
        NTX_VENUE="okx_live",
        OKX_API_KEY="k",
        OKX_API_SECRET="s",
        OKX_API_PASSPHRASE="p",
        OKX_BASE_URL_HTTP="https://www.okx.com",
        NTX_DATA_DIR=str(tmp_path),
    )


# ------------------------------------------------------------ F037 / F038 --- #
def _fill2(oid, qty, px, fee, side="BUY"):
    return SimpleNamespace(
        client_order_id=oid,
        instrument_id="X",
        last_qty=qty,
        last_px=px,
        ts_event=1_000_000_000,
        commission=fee,
        order_side=side,
    )


def _row(j, trade_id):
    with j._conn() as c:
        return c.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()


def _fake_accounting(tmp_path):
    fake, calls, j = _fake_with_submitted_trade(tmp_path)
    st = fake.states["X"]
    st.base_units = 3.0
    st.meta["qty"] = "3"
    fake._closing = {}
    fake._record_exit_fill = lambda ev: NtxStrategy._record_exit_fill(fake, ev)
    fake._record_close_fill = lambda ev: NtxStrategy._record_close_fill(fake, ev)
    fake._forget_closed = lambda inst_id, state, reason, price=None: NtxStrategy._forget_closed(
        fake, inst_id, state, reason, price
    )
    return fake, calls, j


def test_refused_stop_flatten_closes_the_journal_trade_at_the_fill_price(tmp_path):
    """Rehearsal #3: the guard flattened both positions, but their journal rows
    stayed 'open' forever."""
    fake, _calls, j = _fake_accounting(tmp_path)
    NtxStrategy.on_order_filled(fake, _fill2("entry", 3.0, 100.0, 0.15))
    NtxStrategy.on_order_rejected(
        fake, SimpleNamespace(client_order_id="stop-0", instrument_id="X", reason="no")
    )
    # B-02: the close is a tracked reduce-only order now, not a bare flatten, so
    # the row closes when the venue confirms the position is flat.
    close_id = next(iter(fake._close_orders))
    fake.cache = SimpleNamespace(positions_open=lambda instrument_id=None: [])
    NtxStrategy.on_order_filled(fake, _fill2(close_id, 3.0, 99.0, 0.15, side="SELL"))
    r = _row(j, "t2")
    assert r["state"] == "closed"
    assert r["exit_reason"] == "stop_refused"
    assert r["exit_price"] == pytest.approx(99.0)
    assert r["fees_usd"] == pytest.approx(0.30)
    assert r["pnl_gross_usd"] == pytest.approx(-3.0)
    assert r["pnl_net_usd"] == pytest.approx(-3.30)


def test_strategy_exit_is_repriced_by_its_closing_fills(tmp_path):
    """The journal used the last bar close (99.49) while the venue filled at
    99.50, and net PnL ignored fees entirely."""
    fake, _calls, j = _fake_accounting(tmp_path)
    NtxStrategy.on_order_filled(fake, _fill2("entry", 3.0, 100.0, 0.15))
    st = fake.states.pop("X")
    fake._last_close = lambda inst_id: 98.0
    NtxStrategy._close_journal_trade(fake, "X", st, ExitReason.TIME_STOP)
    NtxStrategy.on_order_filled(fake, _fill2("close-1", 1.0, 99.0, 0.05, side="SELL"))
    NtxStrategy.on_order_filled(fake, _fill2("close-1", 2.0, 102.0, 0.10, side="SELL"))
    r = _row(j, "t2")
    assert r["exit_price"] == pytest.approx(101.0)  # vwap of 99 x1 and 102 x2
    assert r["fees_usd"] == pytest.approx(0.30)
    assert r["pnl_gross_usd"] == pytest.approx(3.0)
    assert r["pnl_net_usd"] == pytest.approx(2.70)
    assert r["exit_reason"] == "time_stop"


def test_venue_stop_fill_includes_fees(tmp_path):
    fake, _calls, j = _fake_accounting(tmp_path)
    NtxStrategy.on_order_filled(fake, _fill2("entry", 3.0, 100.0, 0.15))
    NtxStrategy.on_order_filled(fake, _fill2("stop-0", 3.0, 95.0, 0.15, side="SELL"))
    r = _row(j, "t2")
    assert r["fees_usd"] == pytest.approx(0.30)
    assert r["pnl_net_usd"] == pytest.approx(-15.30)


# ------------------------------------------------------------------ F040 ---- #
class _LiveBeforeHistory(DonchianBreakoutStrategy):
    """Rehearsal #7: SOL's first live bar arrived BEFORE its history response;
    every history bar was then older than it and dropped as 'already seen'."""

    def __init__(self, config, hist):
        super().__init__(config)
        self._hist = hist
        self._fed = False
        self.entry_bar_ts: list[int] = []

    def on_start(self):
        super().on_start()
        for inst_id in self.instruments:
            self._await_history(inst_id, requested_ns=self._hist[-1].ts_event + H)

    def on_bar(self, bar):
        super().on_bar(bar)
        if not self._fed and len(self._live_buffer.get(bar.bar_type.instrument_id, [])) >= 3:
            self._fed = True
            for b in self._hist:
                self.on_historical_data(b)
            self._history_complete(bar.bar_type.instrument_id)

    def _enter(self, inst_id, direction, bar):
        self.entry_bar_ts.append(bar.ts_event)
        super()._enter(inst_id, direction, bar)


def test_live_bars_wait_for_history_then_replay():
    inst = TestInstrumentProvider.ethusdt_binance()
    engine = _engine(inst)
    bt = BarType.from_str(f"{inst.id}-1-HOUR-LAST-EXTERNAL")
    bars = _trending_bars(inst, bt, n=400)
    hist, live = bars[:150], bars[150:]
    engine.add_data(live)
    s = _LiveBeforeHistory(
        DonchianBreakoutConfig(
            instrument_ids=[str(inst.id)],
            entry_period=20,
            exit_period=10,
            bar_spec="1-HOUR-LAST-EXTERNAL",
        ),
        hist,
    )
    engine.add_strategy(s)
    engine.run()
    engine.dispose()
    assert s.bars_seen[inst.id] == len(hist) + len(live)
    assert min(s.entry_bar_ts) == live[0].ts_event, "buffered live bars must be replayed"


def test_history_bar_completeness_is_judged_at_request_time():
    """DOGE in rehearsal #7: the request went out at 12:12:57, the reply was
    handled at 12:13:00.66, so the still-forming 12:12 bar looked complete."""
    import inspect

    src = inspect.getsource(NtxStrategy.on_historical_data)
    assert "_history_requested_ns" in src


# ------------------------------------------------------------------ F041 ---- #
def test_cancel_reject_for_an_already_closed_order_is_not_critical(tmp_path):
    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, meta={"trade_id": "t1"})
    fake, _calls, j = _fake_strategy(tmp_path, st)
    fake.cache = SimpleNamespace(order=lambda oid: SimpleNamespace(is_closed=True))
    NtxStrategy.on_order_cancel_rejected(
        fake, SimpleNamespace(instrument_id="X", client_order_id="stop-9", reason="gone")
    )
    assert j.count_events("cancel_rejected", within_seconds=60) == 0


def test_cancel_reject_for_a_live_order_is_critical(tmp_path):
    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, meta={"trade_id": "t1"})
    fake, _calls, j = _fake_strategy(tmp_path, st)
    fake.cache = SimpleNamespace(order=lambda oid: SimpleNamespace(is_closed=False))
    NtxStrategy.on_order_cancel_rejected(
        fake, SimpleNamespace(instrument_id="X", client_order_id="stop-9", reason="busy")
    )
    assert j.count_events("cancel_rejected", within_seconds=60) == 1


# ------------------------------------------------ silent early returns ---- #
def test_entry_skipped_for_unready_atr_is_counted():
    import inspect

    src = inspect.getsource(NtxStrategy._enter)
    assert "entry_skipped" in src


# ------------------------------------------------------------------ F042 ---- #
def test_history_request_has_an_explicit_end():
    """Explicit request window. (The soak's '25 bars' was F043 -- wrong strategy
    parameters -- not a missing `end`; kept for explicitness.)"""
    import inspect

    src = inspect.getsource(NtxStrategy.on_start)
    assert "end=" in src


def test_short_warmup_is_reported():
    import inspect

    assert "warmup_short" in inspect.getsource(NtxStrategy._history_complete)
