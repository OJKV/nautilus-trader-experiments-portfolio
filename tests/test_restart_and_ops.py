"""Gaps G1, G5, G6, G13 and the soak/drill plumbing (written before the code, R41).

G1  restart adoption: a restarted node must take over the positions it holds
G5  preflight: min-notional feasibility must run before trading
G6  live balance / observe-only run
G13 live kill switch
"""

from __future__ import annotations

import pytest
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from test_journal_wiring import _trending_bars
from test_live_node import _settings
from test_live_path import _engine

from ntx.live import node
from ntx.live.reconcile import AdoptAction, VenuePosition, adoption_plan
from ntx.ops import flatten
from ntx.preflight import NoTradableInstruments
from ntx.strategies.base import NtxStrategy
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.signal import Direction

SOL = "SOL-USDT-SWAP.OKX"
ETH = "ETH-USDT-SWAP.OKX"


# ------------------------------------------------ per-strategy identity ---- #
def test_trader_id_and_demo_journal_are_per_strategy(tmp_path):
    """Two nodes (S003 soak + S900 drills) share the demo account. Same
    trader id => colliding clOrdIds; same journal => mixed histories."""
    s = _settings(tmp_path, NTX_VENUE="okx_demo", NTX_EQUITY_OVERRIDE_USD="123.45")
    a = node.plan(s, strategy_id="S003")
    b = node.plan(s, strategy_id="S900")
    assert a.trader_id != b.trader_id
    assert a.trader_id == "NTX-003" and b.trader_id == "NTX-900"
    assert a.journal_path.name == "journal-demo-S003.db"
    assert b.journal_path.name == "journal-demo-S900.db"


def test_live_s003_keeps_the_existing_journal(tmp_path):
    assert node.plan(_settings(tmp_path)).journal_path.name == "journal.db"


def test_reconciliation_is_scoped_to_the_plan_instruments(tmp_path):
    s = _settings(tmp_path)
    p = node.plan(s, instrument_ids=(SOL,))
    cfg = node.node_config(p, s)
    assert [str(i) for i in cfg.exec_engine.reconciliation_instrument_ids] == [SOL]


def test_strategy_claims_its_instruments_after_restart(tmp_path):
    """Without claims, reconciled positions belong to 'EXTERNAL' and the
    strategy never hears their events."""
    s = _settings(tmp_path)
    kw = node.strategy_kwargs(node.plan(s, instrument_ids=(SOL,)), s)
    assert kw["external_order_claims"] == [InstrumentId.from_str(SOL)]


def test_observe_flag_reaches_the_strategy(tmp_path):
    s = _settings(tmp_path)
    kw = node.strategy_kwargs(node.plan(s), s, trading_enabled=False)
    assert kw["trading_enabled"] is False


# ------------------------------------------------------------ preflight ---- #
def test_preflight_narrows_the_plan(tmp_path):
    p = node.plan(_settings(tmp_path), instrument_ids=(SOL, ETH))
    assert node.apply_preflight(p, [ETH]).instrument_ids == (ETH,)


def test_preflight_with_nothing_tradable_refuses(tmp_path):
    p = node.plan(_settings(tmp_path), instrument_ids=(SOL,))
    with pytest.raises(NoTradableInstruments):
        node.apply_preflight(p, [])


def test_main_runs_preflight():
    import inspect

    assert "run_preflight" in inspect.getsource(node.main)


# ------------------------------------------------------------- flatten ---- #
def _pos(inst, qty):
    return {"instId": inst, "pos": qty, "mgnMode": "isolated", "posSide": "net"}


def test_flatten_can_be_scoped_to_instruments():
    rows = [_pos("SOL-USDT-SWAP", "1"), _pos("BTC-USDT-SWAP", "2"), _pos("ETH-USDT-SWAP", "0")]
    assert [r["instId"] for r in flatten.select(rows, None)] == ["SOL-USDT-SWAP", "BTC-USDT-SWAP"]
    assert [r["instId"] for r in flatten.select(rows, {"SOL"})] == ["SOL-USDT-SWAP"]


def test_live_flatten_needs_the_exact_confirmation():
    assert flatten.live_confirmed("FLATTEN LIVE")
    for typed in ("", "yes", "flatten live", "FLATTEN LIVE ", None):
        assert not flatten.live_confirmed(typed)


def test_live_flatten_without_confirmation_sends_nothing(monkeypatch):
    posted = []
    monkeypatch.setattr(flatten, "_signed_post", lambda *a, **k: posted.append(a) or {})
    monkeypatch.setattr(flatten, "_positions", lambda demo: [_pos("SOL-USDT-SWAP", "1")])
    monkeypatch.setattr(flatten, "_egress_ok", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    assert flatten.main(["--live"]) == 2
    assert posted == []


def test_flatten_requires_choosing_an_environment():
    with pytest.raises(SystemExit):
        flatten.main([])


# --------------------------------------------------------- observe only ---- #
def test_observe_only_never_submits():
    inst = TestInstrumentProvider.ethusdt_binance()
    engine = _engine(inst)
    bt = BarType.from_str(f"{inst.id}-1-HOUR-LAST-EXTERNAL")
    engine.add_data(_trending_bars(inst, bt))
    s = DonchianBreakoutStrategy(
        DonchianBreakoutConfig(
            instrument_ids=[str(inst.id)],
            entry_period=20,
            exit_period=10,
            bar_spec="1-HOUR-LAST-EXTERNAL",
            trading_enabled=False,
        )
    )
    engine.add_strategy(s)
    engine.run()
    engine.dispose()
    assert s.entries_submitted == 0
    assert s.entry_skips.get("observe_only", 0) > 0


# ----------------------------------------------------- restart adoption ---- #
def _row(inst=SOL, side="LONG", qty=1.0, stop=95.0, tid="t1"):
    return {
        "instrument_id": inst,
        "side": side,
        "qty": qty,
        "entry_price": 100.0,
        "stop_price": stop,
        "trade_id": tid,
    }


def _vp(inst=SOL, qty=1.0, stop=True):
    return VenuePosition(inst, qty, 100.5, stop)


def _one(plan):
    assert len(plan) == 1
    return plan[0]


def test_known_position_with_live_stop_is_adopted_as_is():
    a = _one(adoption_plan([_vp()], [_row()], {SOL: 94.0}))
    assert a.action is AdoptAction.ADOPT
    assert a.direction is Direction.LONG
    assert a.stop_price == 94.0, "the live venue stop is the truth"
    assert a.trade_id == "t1" and not a.needs_stop
    assert a.entry_price == 100.0, "entry comes from the journal"


def test_known_position_without_stop_gets_the_journal_stop():
    a = _one(adoption_plan([_vp(stop=False)], [_row()], {}))
    assert a.action is AdoptAction.ADOPT and a.needs_stop and a.stop_price == 95.0


def test_unknown_position_with_a_venue_stop_is_adopted():
    a = _one(adoption_plan([_vp(qty=-2.0)], [], {SOL: 110.0}))
    assert a.action is AdoptAction.ADOPT
    assert a.direction is Direction.SHORT and a.quantity == 2.0
    assert a.trade_id is None and a.entry_price == 100.5


def test_unknown_unprotected_position_is_flattened():
    """No journal and no stop: we cannot know where a stop belongs."""
    a = _one(adoption_plan([_vp(stop=False)], [], {}))
    assert a.action is AdoptAction.FLATTEN


def test_side_mismatch_without_venue_stop_is_flattened():
    a = _one(adoption_plan([_vp(qty=-1.0, stop=False)], [_row(side="LONG")], {}))
    assert a.action is AdoptAction.FLATTEN


def test_side_mismatch_with_venue_stop_adopts_venue_side():
    a = _one(adoption_plan([_vp(qty=-1.0)], [_row(side="LONG")], {SOL: 110.0}))
    assert a.action is AdoptAction.ADOPT and a.direction is Direction.SHORT
    assert a.stop_price == 110.0


def test_journal_trade_closed_while_offline_is_closed():
    """A stop that fired while the node was down is normal, not fatal."""
    a = _one(adoption_plan([], [_row()], {}))
    assert a.action is AdoptAction.CLOSE_JOURNAL and a.trade_id == "t1"


def test_venue_quantity_wins():
    a = _one(adoption_plan([_vp(qty=3.0)], [_row(qty=1.0)], {SOL: 94.0}))
    assert a.quantity == 3.0


def test_flat_and_empty_is_nothing_to_do():
    assert adoption_plan([VenuePosition(SOL, 0.0, 0.0, False)], [], {}) == []


def test_strategy_adopts_on_start():
    import inspect

    assert "_adopt_positions" in inspect.getsource(NtxStrategy.on_start)
    assert "adoption_plan" in inspect.getsource(NtxStrategy._adopt_positions)


# ---------------------------------------------------------------- F044 ---- #
def test_released_venue_stop_is_resubmitted_at_the_same_price():
    """Drill A: nautilus reconciliation does not load OKX algo stops. Adoption
    saw 'no stop', OKX refused a second close-position stop, the guard
    flattened the adopted position. Now the old stop is released (cancelled via
    REST) and its price re-used for a fresh, tracked stop."""
    a = _one(adoption_plan([_vp(stop=False)], [_row()], {}, released_stops={SOL: 94.0}))
    assert a.action is AdoptAction.ADOPT and a.needs_stop and a.stop_price == 94.0


def test_released_stop_protects_an_unknown_position():
    a = _one(adoption_plan([_vp(qty=-1.0, stop=False)], [], {}, released_stops={SOL: 110.0}))
    assert a.action is AdoptAction.ADOPT and a.needs_stop and a.stop_price == 110.0


def test_strategy_asks_the_hook_for_untracked_venue_stops():
    import inspect

    assert "venue_stop_hook" in inspect.getsource(NtxStrategy._adopt_positions)


def test_node_installs_the_venue_stop_hook():
    import inspect

    assert "venue_stop_hook" in inspect.getsource(node.main)


# ------------------------------------------- drill B follow-up ------------- #
def test_external_stop_cancel_is_repaired_at_once():
    """Drill B: a stop cancelled by hand was repaired only at the next watchdog
    tick (20 min in production). A cancel we did not ask for re-attaches now."""
    from types import SimpleNamespace

    from ntx.strategies.exits import PositionState

    calls = []
    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, meta={"filled_qty": 2.0})
    fake = SimpleNamespace(
        states={"X": st},
        stop_orders={"X": SimpleNamespace(client_order_id="s1")},
        stop_client_order_ids={"s1": "X"},
        journal=None,
        unprotected_events=0,
        log=SimpleNamespace(info=print, warning=print, error=print),
        _submit_stop=lambda inst, state, qty: calls.append((inst, qty)),
        _has_position=lambda inst: True,
    )
    fake._jevent = NtxStrategy._jevent.__get__(fake, NtxStrategy)
    fake._jwrite = NtxStrategy._jwrite.__get__(fake, NtxStrategy)
    NtxStrategy.on_order_canceled(fake, SimpleNamespace(client_order_id="s1", instrument_id="X"))
    assert calls == [("X", 2.0)]
    assert fake.unprotected_events == 1


def test_our_own_cancel_is_not_repaired():
    from types import SimpleNamespace

    calls = []
    fake = SimpleNamespace(
        states={},
        stop_orders={},
        stop_client_order_ids={},
        journal=None,
        log=SimpleNamespace(info=print, warning=print, error=print),
        _submit_stop=lambda *a: calls.append(a),
        _has_position=lambda inst: True,
        _entry_canceled=lambda ev: None,
    )
    NtxStrategy.on_order_canceled(fake, SimpleNamespace(client_order_id="s1", instrument_id="X"))
    assert calls == []


def test_cancel_after_the_position_is_gone_is_not_repaired():
    """OKX cancels a close-position stop itself when the position is closed
    elsewhere (e.g. by hand); re-attaching would only be rejected."""
    from types import SimpleNamespace

    from ntx.strategies.exits import PositionState

    calls = []
    st = PositionState(Direction.LONG, 100.0, 95.0, 10.0, meta={"filled_qty": 2.0})
    fake = SimpleNamespace(
        states={"X": st},
        stop_orders={"X": SimpleNamespace(client_order_id="s1")},
        stop_client_order_ids={"s1": "X"},
        journal=None,
        unprotected_events=0,
        log=SimpleNamespace(info=print, warning=print, error=print),
        _submit_stop=lambda inst, state, qty: calls.append((inst, qty)),
        _has_position=lambda inst: False,
    )
    NtxStrategy.on_order_canceled(fake, SimpleNamespace(client_order_id="s1", instrument_id="X"))
    assert calls == []


def test_observe_mode_never_acts_on_adoption():
    """--observe on the LIVE account must not flatten, attach or cancel anything."""
    import inspect

    src = inspect.getsource(NtxStrategy._adopt_positions)
    assert "trading_enabled" in src
    assert "args.observe" in inspect.getsource(node.main).split("venue_stop_hook")[0] or (
        "if not args.observe" in inspect.getsource(node.main)
    )


# ---------------------------------------------------------------- F045 ---- #
def test_live_equity_reads_the_usdt_balance_of_a_multi_currency_account():
    """Live observe run: account base_currency=None (USDT + SOL/BTC dust), and
    balance_total() without a currency raised TypeError at start."""
    from types import SimpleNamespace

    from nautilus_trader.model.currencies import USDT

    def balance_total(currency=None):
        if currency is None:
            raise TypeError("'currency' argument was `None`")
        assert currency == USDT
        return SimpleNamespace(as_double=lambda: 37.32)

    account = SimpleNamespace(base_currency=None, balance_total=balance_total)
    fake = SimpleNamespace(
        config=SimpleNamespace(equity_override_usd=None),
        instrument_ids=[InstrumentId.from_str(SOL)],
        portfolio=SimpleNamespace(account=lambda venue: account, unrealized_pnls=lambda venue: {}),
        states={},
        equity_hook=None,  # a backtest-shaped read: the account balance itself
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
    )
    fake._equity_or_none = NtxStrategy._equity_or_none.__get__(fake, NtxStrategy)
    assert NtxStrategy._equity_usd(fake) == pytest.approx(37.32)


def test_preflight_equity_is_the_usdt_equity_not_total_with_dust():
    bal = {
        "code": "0",
        "data": [
            {
                "totalEq": "37.75",
                "details": [
                    {"ccy": "SOL", "eq": "0.0043", "eqUsd": "0.43"},
                    {"ccy": "USDT", "eq": "37.32", "eqUsd": "37.32"},
                ],
            }
        ],
    }
    assert node.usdt_equity(bal) == pytest.approx(37.32)


def test_preflight_equity_without_usdt_is_zero():
    assert node.usdt_equity({"code": "0", "data": [{"details": []}]}) == 0.0


# ----------------------------------------------------- L-09 / L-10 (R-005) -- #
def test_adoption_does_not_cancel_a_stop_it_is_not_about_to_replace():
    """L-10: every stop was cancelled up front and re-placed later in the loop.
    With two positions the first one is naked for the whole of the second one's
    REST round trip. Planning must be READ-ONLY; the cancel belongs next to the
    re-place, one instrument at a time.

    This is the normal path on OKX, not an edge case: algo stops are not in the
    cache at startup (F044), so every restart with a position goes through it.
    """
    from types import SimpleNamespace

    from ntx.strategies.base import NtxStrategy

    order = []

    def peek(inst_id):
        order.append(("peek", inst_id))
        return 95.0

    def release(inst_id):
        order.append(("release", inst_id))
        return 95.0

    positions = {
        "A": SimpleNamespace(signed_qty=1.0, avg_px_open=100.0, quantity=1.0),
        "B": SimpleNamespace(signed_qty=1.0, avg_px_open=100.0, quantity=1.0),
    }
    fake = SimpleNamespace(
        instruments={"A": None, "B": None},
        instrument_ids=["A", "B"],
        states={},
        stop_orders={},
        stop_client_order_ids={},
        journal=None,
        atr={},
        exit_cfg=SimpleNamespace(tsl_enabled=False),
        config=SimpleNamespace(trading_enabled=True, max_leverage=3.0),
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: (
                [positions[instrument_id]] if instrument_id in positions else []
            ),
            orders_open=lambda instrument_id=None: [],
        ),
        venue_stop_peek_hook=peek,
        venue_stop_hook=release,
        _submit_stop=lambda inst, state, qty: order.append(("place", inst)),
        _flatten=lambda inst: order.append(("flatten", inst)),
        _stop_attached_at_entry=lambda: False,
        id="S003",
    )
    # Adoption now runs one position per event-loop turn (L2-04), so the stub
    # needs a clock and the test drives the timer itself.
    timers: dict = {}
    fake.clock = SimpleNamespace(
        set_timer=lambda name, interval, callback: timers.__setitem__(name, callback),
        cancel_timer=lambda name: timers.pop(name, None),
    )
    fake.ADOPT_TICK_SECS = NtxStrategy.ADOPT_TICK_SECS
    fake._adopt_queue = []
    fake._adopt_timer_on = False
    for _n in (
        "_adopt",
        "_jwrite",
        "_jevent",
        "_drain_adoptions",
        "_schedule_adoption_timer",
        "_cancel_adoption_timer",
        # F065: adoption reaps stops that guard no position before it plans.
        "_reap_orphan_stops",
        # F066: a "closed while offline" is checked at the venue first.
        "_confirm_venue_flat",
        "_venue_checked",
    ):
        setattr(fake, _n, getattr(NtxStrategy, _n).__get__(fake, NtxStrategy))
    fake.instruments = {"A": SimpleNamespace(multiplier=1.0), "B": SimpleNamespace(multiplier=1.0)}
    NtxStrategy._adopt_positions(fake)
    while timers.get("ntx-adoption"):
        timers["ntx-adoption"](None)

    assert [o for o in order if o[0] == "peek"] == [("peek", "A"), ("peek", "B")]
    for inst in ("A", "B"):
        rel = order.index(("release", inst))
        place = order.index(("place", inst))
        assert place == rel + 1, f"{inst}: something happened between cancel and re-place: {order}"
    assert ("flatten", "A") not in order and ("flatten", "B") not in order


def test_a_failed_stop_lookup_never_flattens_a_healthy_position():
    """L-09: `except Exception: price = None` made "the venue did not answer"
    indistinguishable from "there is no stop", and the plan then flattens a
    position that is perfectly protected. Refuse to start instead: the venue stop
    is still in place, and systemd will retry."""
    from types import SimpleNamespace

    import pytest as _pytest

    from ntx.strategies.base import NtxStrategy

    acted = []
    pos = SimpleNamespace(signed_qty=1.0, avg_px_open=100.0, quantity=1.0)

    def boom(inst_id):
        raise OSError("okx timeout")

    fake = SimpleNamespace(
        instruments={"A": None},
        instrument_ids=["A"],
        states={},
        stop_orders={},
        stop_client_order_ids={},
        journal=None,
        atr={},
        exit_cfg=SimpleNamespace(tsl_enabled=False),
        config=SimpleNamespace(trading_enabled=True, max_leverage=3.0),
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: [pos] if instrument_id == "A" else [],
            orders_open=lambda instrument_id=None: [],
        ),
        venue_stop_peek_hook=boom,
        venue_stop_hook=boom,
        _submit_stop=lambda inst, state, qty: acted.append(("place", inst)),
        _flatten=lambda inst: acted.append(("flatten", inst)),
        _stop_attached_at_entry=lambda: False,
        id="S003",
    )
    for _n in (
        "_adopt",
        "_jwrite",
        "_jevent",
        "_reap_orphan_stops",
        "_confirm_venue_flat",
        "_venue_checked",
    ):
        setattr(fake, _n, getattr(NtxStrategy, _n).__get__(fake, NtxStrategy))
    with _pytest.raises(RuntimeError, match="stop"):
        NtxStrategy._adopt_positions(fake)
    assert acted == [], "nothing may be sent to the venue on unreliable information"
