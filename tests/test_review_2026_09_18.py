"""Pre-live review (docs/private/code-review-2026-09-18.md) -- reproductions.

Every finding confirmed as a real defect is pinned by a test written BEFORE the
fix (R41). Ids match the review: B- base strategy, L- live/ops, R- risk/config,
O- observability.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from nautilus_trader.indicators.volatility import AverageTrueRange
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from ntx.strategies.base import NtxStrategy
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.exits import PositionState
from ntx.strategies.signal import Direction, ExitReason

ROOT = Path(__file__).resolve().parents[1]
H4 = 4 * 3_600_000_000_000


# ------------------------------------------------------------------ config -- #
def _env_file(tmp_path, body: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(body)
    return p


def test_r03_blank_equity_override_loads_as_none(tmp_path):
    """docs/21 tells the operator to leave the line blank; that crashed Settings,
    and every tool that loads settings with it -- including `flatten --live`."""
    from ntx.config.settings import Settings

    env = _env_file(
        tmp_path,
        "NTX_VENUE=okx_live\nOKX_API_KEY=k\nOKX_API_SECRET=s\n"
        "OKX_API_PASSPHRASE=p\nNTX_EQUITY_OVERRIDE_USD=\n",
    )
    s = Settings(_env_file=env)
    assert s.equity_override_usd is None


def test_b06_live_run_refuses_an_equity_override(tmp_path):
    """A frozen equity means size never shrinks (R5) and both loss guards see
    equity == high-water == day-start, so they can never fire."""
    from ntx.config.settings import Settings
    from ntx.live.node import plan

    env = _env_file(
        tmp_path,
        "NTX_VENUE=okx_live\nOKX_API_KEY=k\nOKX_API_SECRET=s\n"
        "OKX_API_PASSPHRASE=p\nNTX_EQUITY_OVERRIDE_USD=37.0\n",
    )
    with pytest.raises(RuntimeError, match="OVERRIDE"):
        plan(Settings(_env_file=env))


def test_l11_a_venue_string_that_is_not_exactly_demo_or_live_is_refused(tmp_path):
    """`endswith('_demo')` made okx-demo / okx_paper / okxdemo mean LIVE keys."""
    from ntx.config.settings import Settings

    for bad in ("okx-demo", "okxdemo", "okx_paper"):
        env = _env_file(tmp_path, f"NTX_VENUE={bad}\n")
        with pytest.raises(ValueError, match="NTX_VENUE"):
            Settings(_env_file=env)


def test_l04_demo_and_live_use_different_lock_files(tmp_path):
    """docs/21 runs the demo soak and the live node on one box; one shared lock
    file means whichever starts second ends `failed` -- possibly the live one."""
    from ntx.live.lock import single_instance

    with (
        single_instance("NTX-003", tmp_path, demo=True),
        single_instance("NTX-003", tmp_path, demo=False),
    ):
        pass


def test_l03_a_stale_pid_from_another_process_does_not_wedge_the_unit(tmp_path):
    """After a hard reboot the recorded pid is some unrelated daemon. The lock
    is held by the kernel now (flock), so a dead holder never blocks a start."""
    from ntx.live.lock import single_instance

    (tmp_path / "locks").mkdir()
    (tmp_path / "locks" / "NTX-003-live.lock").write_text("1")  # pid 1 is always alive
    with single_instance("NTX-003", tmp_path, demo=False):
        pass


# -------------------------------------------------------------- alerting --- #
def _snapshot(tmp_path, bot="S003-demo", age_s=5.0, **over):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    snap = {
        "schema": 1,
        "ts_ns": int((time.time() - age_s) * 1e9),
        "strategy_id": bot.split("-")[0],
        "positions_unprotected": 0,
        "positions": [],
    }
    snap.update(over)
    p = d / f"{bot}.json"
    p.write_text(json.dumps(snap))
    return p


def test_o03_a_position_without_a_live_stop_alerts_even_when_the_counter_says_zero():
    """The counter was dict membership only; `positions[].protected` is the one
    that knows whether the stop is still alive at the venue (R15)."""
    from ntx.obs.alerts import alerts_from_snapshot

    snap = {
        "ts_ns": int(time.time() * 1e9),
        "positions_unprotected": 0,
        "positions": [{"instrument": "SOL-USDT-SWAP.OKX", "protected": False}],
    }
    got = alerts_from_snapshot("S003-live", snap, now=time.time(), max_age_s=120)
    assert any(a.kind == "positions_unprotected" and a.severity == "critical" for a in got)


def test_o06_a_future_dated_snapshot_is_not_fresh(tmp_path):
    from ntx.obs.alerts import build_alerts

    _snapshot(tmp_path, age_s=-3600)
    alerts = build_alerts(tmp_path, max_age_s=120)
    assert any(a.kind == "snapshot_stale" for a in alerts)


def test_o06_an_unreadable_journal_is_a_critical(tmp_path):
    from ntx.obs.alerts import build_alerts

    (tmp_path / "state").mkdir()
    bad = tmp_path / "journal-demo-S003.db"
    bad.write_text("not a database")
    alerts = build_alerts(tmp_path, max_age_s=120, journals=[bad])
    assert any(a.kind == "journal_unreadable" and a.severity == "critical" for a in alerts)


def test_o01_a_failed_delivery_does_not_advance_the_cursor(tmp_path, monkeypatch):
    """ntfy 429 at the moment of a guard_KILL used to lose the alert forever and
    then ping the dead-man switch healthy."""
    from ntx.obs import alerts as mod
    from ntx.obs.journal import Journal

    j = Journal(tmp_path / "journal-demo-S003.db")
    j.event("guard_kill", severity="critical", reason="drawdown")
    _snapshot(tmp_path)
    monkeypatch.setattr(mod, "_post", lambda *a, **k: (_ for _ in ()).throw(OSError("429")))
    settings = SimpleNamespace(
        data_dir=tmp_path,
        alert_ntfy_url="https://ntfy.sh/t",
        healthcheck_url="https://hc-ping.com/u",
        alert_bots="S003-demo",
        alert_fills=True,
    )
    monkeypatch.setattr("ntx.config.settings.load", lambda: settings)

    rc = mod.main([])
    assert rc != 0, "a failed delivery must not report success"
    assert mod.load_cursor(tmp_path, "S003-demo") == 0, "undelivered alert must stay pending"


def test_o02_an_empty_alert_bots_list_is_itself_a_critical(tmp_path, monkeypatch):
    """With nothing declared, a dead bot is 'retired' after 24 h: alerts stop and
    the dead-man switch turns green with a position possibly still open."""
    from ntx.obs import alerts as mod

    posted: list = []
    monkeypatch.setattr(mod, "_post", lambda url, data, headers=None: posted.append(url))
    (tmp_path / "state").mkdir()
    settings = SimpleNamespace(
        data_dir=tmp_path,
        alert_ntfy_url="https://ntfy.sh/t",
        healthcheck_url="https://hc-ping.com/u",
        alert_bots="",
        alert_fills=True,
    )
    monkeypatch.setattr("ntx.config.settings.load", lambda: settings)

    mod.main([])
    assert any("/fail" in u for u in posted), "nothing declared: never ping healthy"


def test_o02_a_healthy_ping_needs_at_least_one_fresh_expected_bot(tmp_path, monkeypatch):
    from ntx.obs import alerts as mod

    posted: list = []
    monkeypatch.setattr(mod, "_post", lambda url, data, headers=None: posted.append(url))
    _snapshot(tmp_path, bot="S003-demo", age_s=5)
    settings = SimpleNamespace(
        data_dir=tmp_path,
        alert_ntfy_url="",
        healthcheck_url="https://hc-ping.com/u",
        alert_bots="S003-demo",
        alert_fills=True,
    )
    monkeypatch.setattr("ntx.config.settings.load", lambda: settings)
    assert mod.main([]) == 0
    assert posted == ["https://hc-ping.com/u"]


# -------------------------------------------------------------- warm-up ---- #
def _bar(bar_type, ts, o, h, low, c):
    return Bar(
        bar_type=bar_type,
        open=Price.from_str(f"{o:.2f}"),
        high=Price.from_str(f"{h:.2f}"),
        low=Price.from_str(f"{low:.2f}"),
        close=Price.from_str(f"{c:.2f}"),
        volume=Quantity.from_str("10"),
        ts_event=ts,
        ts_init=ts,
    )


def _warm(strategy, inst_id, n: int):
    bar_type = BarType.from_str(f"{inst_id}-4-HOUR-LAST-EXTERNAL")
    strategy.instruments[inst_id] = None
    strategy.atr[inst_id] = AverageTrueRange(14)
    strategy.bars_seen[inst_id] = 0
    strategy._last_bar_ts.pop(inst_id, None)
    strategy.on_indicators_init(inst_id)
    for i in range(n):
        px = 100.0 + i
        strategy._ingest(_bar(bar_type, (i + 1) * H4, px, px + 1, px - 1, px))


def test_r01_the_entry_channel_is_ready_as_soon_as_the_warmup_history_is_in():
    """`warmup + 5` history bars must leave a usable 55-bar channel. It did not:
    the channel was only appended to inside `on_bar_signal`, which runs after the
    warm-up gate -- so after a restart S003 could not enter for 8.2 days while
    the monitor reported `warm: true`."""
    inst_id = InstrumentId.from_str("SOL-USDT-SWAP.OKX")
    s = DonchianBreakoutStrategy(
        DonchianBreakoutConfig(
            instrument_ids=[str(inst_id)],
            entry_period=55,
            exit_period=20,
            bar_spec="4-HOUR-LAST-EXTERNAL",
            request_warmup=True,
        )
    )
    _warm(s, inst_id, s.warmup_bars() + 5)
    assert s.channel(inst_id, 55) is not None, "no entry possible after a restart"
    assert s.channel(inst_id, 20) is not None


def test_r01_the_channel_still_excludes_the_current_bar():
    """The fix must not turn `close > channel_high` into a tautology."""
    inst_id = InstrumentId.from_str("SOL-USDT-SWAP.OKX")
    s = DonchianBreakoutStrategy(
        DonchianBreakoutConfig(
            instrument_ids=[str(inst_id)], entry_period=5, exit_period=2, atr_period=3
        )
    )
    bar_type = BarType.from_str(f"{inst_id}-4-HOUR-LAST-EXTERNAL")
    s.instruments[inst_id] = None
    s.atr[inst_id] = AverageTrueRange(3)
    s.bars_seen[inst_id] = 0
    s.on_indicators_init(inst_id)
    for i in range(6):
        s._ingest(_bar(bar_type, (i + 1) * H4, 100, 101, 99, 100))
    hi, _lo = s.channel(inst_id, 5)
    assert hi == 101
    s._ingest(_bar(bar_type, 7 * H4, 100, 500, 99, 100))  # a spike bar
    hi_after, _ = s.channel(inst_id, 5)
    assert hi_after == 101, "the current bar must not be in its own channel"


def test_r01_readiness_is_reported_per_instrument():
    """The snapshot said `warm: true` while no instrument could produce a signal.
    Readiness must mean "a signal is possible", not "enough bars were counted"."""
    inst_id = InstrumentId.from_str("SOL-USDT-SWAP.OKX")
    s = DonchianBreakoutStrategy(
        DonchianBreakoutConfig(instrument_ids=[str(inst_id)], entry_period=55, exit_period=20)
    )
    _warm(s, inst_id, 10)
    assert s.signal_ready(inst_id) is False
    _warm(s, inst_id, s.warmup_bars() + 5)
    assert s.signal_ready(inst_id) is True


# ------------------------------------------------------- strategy safety --- #
def _fake(tmp_path, **over):
    """A strategy stub with one open, protected LONG (the codex-review pattern)."""
    from ntx.obs.journal import Journal

    calls = SimpleNamespace(submitted=[], cancelled=[], flattened=[])
    j = Journal(tmp_path / "j.db")
    j.open_trade(trade_id="t1", correlation_id="c", strategy_id="S", venue="OKX",
                 instrument_id="X", side="LONG", qty=1.0, entry_price=100.0)  # fmt: skip
    st = PositionState(
        Direction.LONG, 100.0, 95.0, 10.0, base_units=1.0,
        meta={"trade_id": "t1", "entry_order_id": "entry", "filled_qty": 1.0,
              "covered_qty": 1.0, "qty": "1"},
    )  # fmt: skip

    def market(instrument_id, order_side, quantity, time_in_force=None, reduce_only=False):
        return SimpleNamespace(
            client_order_id=f"close-{len(calls.submitted) + 1}",
            side=order_side, quantity=quantity, reduce_only=reduce_only,
        )  # fmt: skip

    fake = SimpleNamespace(
        states={"X": st},
        stop_orders={"X": SimpleNamespace(client_order_id="stop-1")},
        stop_client_order_ids={"stop-1": "X"},
        exit_counts={},
        entry_skips={},
        config=SimpleNamespace(trading_enabled=True),
        journal=j,
        close_failures=0,
        unprotected_events=0,
        stops_refused=0,
        entries_refused=0,
        _killed=False,
        _close_orders={},
        _closing={},
        instruments={"X": SimpleNamespace(make_qty=lambda q: q)},
        order_factory=SimpleNamespace(market=market),
        submit_order=lambda o, params=None: calls.submitted.append(o),
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: [SimpleNamespace(quantity=1.0)]
        ),
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        _cancel_protective_stop=lambda inst: calls.cancelled.append(inst),
        _flatten=lambda inst: calls.flattened.append(inst),
        _last_close=lambda inst: 99.0,
        _equity_usd=lambda: 100.0,
        _stop_is_live=lambda order: True,
        _fee=NtxStrategy._fee,
        _entry_px=NtxStrategy._entry_px,
        _pnl=NtxStrategy._pnl,
    )
    fake.__dict__.update(over)
    fake.DUPLICATE_STOP_SIGNS = NtxStrategy.DUPLICATE_STOP_SIGNS
    for name in (
        "_exit", "_submit_close", "_venue_close_qty", "_position_qty", "_record_close_fill", "_close_confirmed",
        "_close_refused", "_close_journal_trade", "_handle_refused", "_forget_closed",
        "_already_protected",
        "positions_unprotected_count", "_jwrite", "_jevent", "_venue_writes_allowed",
    ):  # fmt: skip
        attr = getattr(NtxStrategy, name)
        setattr(fake, name, attr.__get__(fake, NtxStrategy))
    return fake, calls, j


def test_b02_a_refused_stop_closes_through_the_confirmed_close_path(tmp_path):
    """C-01 regression on one branch: the stop-refused path popped the state and
    fired an untracked `close_position`, so a failed flatten left a naked,
    invisible position with no retry."""
    fake, calls, _j = _fake(tmp_path)
    fake._handle_refused(
        SimpleNamespace(client_order_id="stop-1", instrument_id="X", reason="rejected"), "rejected"
    )
    assert "X" in fake.states, "the position must not be forgotten before it is flat"
    assert fake.states["X"].meta.get("closing") == str(ExitReason.STOP_REFUSED)
    order = calls.submitted[-1]
    assert fake._close_orders[order.client_order_id] == "X", "the close must be tracked and retried"


def test_b03_a_dead_stop_counts_as_unprotected(tmp_path):
    """`positions_unprotected` was dict membership only: a stop cancelled at the
    venue during a websocket gap still read as protected."""
    fake, _calls, _j = _fake(tmp_path, _stop_is_live=lambda order: False)
    assert fake.positions_unprotected_count() == 1


def test_b03_a_venue_position_without_a_state_counts_as_unprotected(tmp_path):
    """The metric that exists to catch orphans could not see one."""
    fake, _calls, _j = _fake(tmp_path)
    fake.states.clear()
    fake.instrument_ids = ["X"]
    assert fake.positions_unprotected_count() == 1


def test_b01_the_position_state_exists_before_the_entry_order_is_sent():
    """A journal write between submit and state assignment could orphan a live
    position: the fill found no state, so no stop was ever placed."""
    import inspect

    src = inspect.getsource(NtxStrategy._enter)
    submit = src.index("_submit_entry(")
    assign = src.index("self.states[inst_id] = PositionState(")
    assert assign < submit, "state must exist before the order reaches the venue"
    assert "_jwrite(" in src, "journal calls in the entry path must never raise"


def test_b04_kill_flattens_without_waiting_for_a_new_entry_signal(tmp_path):
    """Guards ran only inside `_enter`, so a 25% drawdown did nothing until some
    other instrument produced a Donchian breakout -- which can be days."""
    fake, _calls, _j = _fake(tmp_path)
    fake.config = SimpleNamespace(
        daily_loss_limit_equity_pct=5.0, max_drawdown_equity_pct=20.0, state_path=None,
        equity_override_usd=70.0, trading_enabled=True,
    )  # fmt: skip
    fake._equity_usd = lambda: 70.0
    fake._high_water = 100.0
    fake._day_start_equity = 100.0
    fake._day = "2026-09-19"
    fake.clock = SimpleNamespace(
        utc_now=lambda: __import__("datetime").datetime(2026, 9, 19),
        timestamp_ns=lambda: 0,
    )
    for name in ("_loss_verdict", "_risk_check", "_kill_all", "_kill_latch_path",
                 "_latch_kill", "_equity_or_none"):  # fmt: skip
        setattr(fake, name, getattr(NtxStrategy, name).__get__(fake, NtxStrategy))
    fake._risk_check()
    assert fake._killed is True
    assert fake.states["X"].meta.get("closing") == str(ExitReason.KILL_SWITCH)


def test_b05_the_high_water_mark_survives_a_restart(tmp_path):
    """HWM lived in memory only: after a restart an account already 19% down
    could lose another 20% before KILL, repeatedly."""
    from ntx.obs.journal import Journal

    j = Journal(tmp_path / "j.db")
    j.record_equity(100.0)
    j.record_equity(80.0)
    fake = SimpleNamespace(journal=j, _high_water=0.0, _killed=False,
                           _equity_or_none=lambda: 100.0, _jevent=lambda *a, **k: None,
                           log=SimpleNamespace(info=print, warning=print, error=print))  # fmt: skip
    fake.config = SimpleNamespace(state_path=str(tmp_path / "s.json"), is_demo=False)
    fake._kill_latch_path = NtxStrategy._kill_latch_path.__get__(fake, NtxStrategy)
    NtxStrategy._seed_risk_state(fake)
    assert fake._high_water == 100.0


def test_b05_a_kill_is_latched_across_a_restart(tmp_path):
    """A KILL was forgotten on restart: trading resumed as if nothing happened."""
    state = tmp_path / "S003-live.json"
    fake = SimpleNamespace(journal=None, _high_water=0.0, _killed=False,
                           _equity_or_none=lambda: 100.0, _jevent=lambda *a, **k: None,
                           log=SimpleNamespace(info=print, warning=print, error=print))  # fmt: skip
    fake.config = SimpleNamespace(state_path=str(state), is_demo=False)
    fake._kill_latch_path = NtxStrategy._kill_latch_path.__get__(fake, NtxStrategy)
    NtxStrategy._latch_kill(fake)
    fresh = SimpleNamespace(journal=None, _high_water=0.0, _killed=False,
                            _equity_or_none=lambda: 100.0, _jevent=lambda *a, **k: None,
                            log=SimpleNamespace(info=print, warning=print, error=print))  # fmt: skip
    fresh.config = fake.config
    fresh._kill_latch_path = NtxStrategy._kill_latch_path.__get__(fresh, NtxStrategy)
    NtxStrategy._seed_risk_state(fresh)
    assert fresh._killed is True


def test_b07_unknown_equity_pauses_instead_of_killing_everything():
    """`_equity_usd` returns 0.0 when the balance is not in the cache yet and
    `guards.py` maps 0 to KILL -> a reconnect blip flattened the book."""
    from ntx.risk.guards import Verdict

    fake = SimpleNamespace(
        _equity_or_none=lambda: None,
        _high_water=100.0,
        _day_start_equity=100.0,
        _day="2026-09-19",
        config=SimpleNamespace(
            daily_loss_limit_equity_pct=5.0, max_drawdown_equity_pct=20.0, equity_override_usd=None
        ),
        clock=SimpleNamespace(utc_now=lambda: __import__("datetime").datetime(2026, 9, 19)),
        log=SimpleNamespace(info=print, warning=print, error=print),
    )
    verdict, why = NtxStrategy._loss_verdict(fake)
    assert verdict is Verdict.PAUSE, why


def test_o05_every_start_is_journalled_so_a_restart_loop_is_visible():
    """The restart gap (30 s) is shorter than the 120 s staleness limit, so a
    node crashing every few minutes looked healthy."""
    import inspect

    src = inspect.getsource(NtxStrategy.on_start)
    assert "node_started" in src


# --------------------------------------------------------------- process --- #
def test_l01_an_internal_shutdown_exits_non_zero():
    """`graceful_shutdown_on_exception` makes `run()` return normally, so an
    internal error exited 0: systemd saw success, never restarted, and the
    `OnFailure=` crash alert never fired."""
    from ntx.live.node import run_exit_code

    assert run_exit_code(signalled=True) == 0
    assert run_exit_code(signalled=False) != 0


def test_l05_preflight_keeps_an_instrument_that_holds_a_position():
    """Preflight narrows the universe from current equity; an instrument that
    was dropped is not subscribed, not adopted and never exits."""
    from ntx.config.settings import Settings
    from ntx.live.node import apply_preflight, plan

    s = Settings(
        _env_file=None, NTX_VENUE="okx_live", OKX_API_KEY="k", OKX_API_SECRET="s",
        OKX_API_PASSPHRASE="p",
    )  # fmt: skip
    p = plan(s, instrument_ids=("SOL-USDT-SWAP.OKX", "LINK-USDT-SWAP.OKX"))
    out = apply_preflight(p, ["SOL-USDT-SWAP.OKX"], held=("LINK-USDT-SWAP.OKX",))
    assert "LINK-USDT-SWAP.OKX" in out.instrument_ids


def test_l06_the_kill_switch_can_be_run_when_the_geo_lookup_fails():
    """During an incident -- exactly when networks misbehave -- the flatten tool
    refused to run because a third-party geo lookup timed out."""
    from ntx.ops.flatten import egress_gate

    assert egress_gate(skip=True, check=lambda: False) is True
    assert egress_gate(skip=False, check=lambda: False) is False


def test_l16_the_makefile_carries_no_server_address():
    """A tracked, pushed file must not name the VPS user or IP (docs 21/22 are
    gitignored for exactly that reason)."""
    import re

    text = (ROOT / "Makefile").read_text()
    public = [
        ip
        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
        if not ip.startswith(("127.", "0.0.0.0", "10.", "192.168."))
    ]
    assert not public, f"server address in a tracked file: {public}"


def test_d08_the_live_unit_pins_the_attached_stop_flag_off():
    """Both units read the same .env; the soak's flag must not leak into live."""
    unit = (ROOT / "deploy" / "ntx-s003.service").read_text()
    assert "NTX_ATTACH_STOP_AT_ENTRY=false" in unit


# ------------------------------------------------------------------- L-08 --- #
def test_l08_observe_mode_sends_no_command_to_the_venue():
    """R48: `--observe` never orders, never adopts, NEVER CANCELS.

    The cancel half was unenforced: `_protection_watchdog` -> `_sweep_stale_stops`
    calls `cancel_order` with no `trading_enabled` check, and in observe mode
    `stop_client_order_ids` is empty while `external_order_claims` still hands us
    the account's real stops -- so every one of them looked untracked and would
    have been cancelled 20 minutes in. Found by re-reading the 2026-09-19 live
    observe run, which only escaped it because it lasted 97 seconds.
    """
    sent = []
    stop = SimpleNamespace(client_order_id="venue-stop-1", order_type=SimpleNamespace(name="STOP_MARKET"), is_closed=False)  # fmt: skip
    fake = SimpleNamespace(
        config=SimpleNamespace(trading_enabled=False, bar_spec="4-HOUR-LAST-EXTERNAL"),
        instruments={"X": None},
        instrument_ids=["X"],
        states={},
        stop_orders={},
        stop_client_order_ids={},
        journal=None,
        unprotected_events=0,
        id="S003",
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        cache=SimpleNamespace(
            orders_open=lambda instrument_id=None, strategy_id=None: [stop],
            order=lambda oid: stop,
            positions_open=lambda instrument_id=None: [],
        ),
        cancel_order=lambda o: sent.append(("cancel", o)),
        cancel_all_orders=lambda i: sent.append(("cancel_all", i)),
        close_position=lambda p: sent.append(("close", p)),
        submit_order=lambda o, params=None: sent.append(("submit", o)),
    )
    for name in ("_sweep_stale_stops", "_cancel_order_id", "_cancel_protective_stop",
                 "_flatten", "_venue_writes_allowed", "_jwrite", "_jevent"):  # fmt: skip
        setattr(fake, name, getattr(NtxStrategy, name).__get__(fake, NtxStrategy))

    fake._sweep_stale_stops()
    fake._flatten("X")
    fake._cancel_protective_stop("X")
    assert sent == [], f"observe mode touched the venue: {sent}"


# --------------------------------------------------------------- R40/caps --- #
def test_a_position_shrunk_by_a_cap_is_counted_not_silent():
    """`max_leverage` and the per-asset cap SHRINK an order rather than reject
    it, and nothing recorded that. A 1x run and a 3x run then look identical in
    the results table while the 1x one is quietly trading smaller (R40).
    """
    from ntx.data.instruments import build_instrument
    from ntx.risk.sizing import SizingLimits, size_position

    inst = build_instrument(
        {
            "instId": "SOL-USDT-SWAP",
            "ctVal": "1",
            "ctValCcy": "SOL",
            "lotSz": "0.01",
            "minSz": "0.01",
            "tickSz": "0.001",
            "ctMult": "1",
        }
    )
    # A very tight stop asks for a huge notional: $100 x 0.5% / 0.05 = $1000.
    tight = size_position(
        instrument=inst,
        price=100.0,
        stop_distance=0.05,
        equity_usd=100.0,
        limits=SizingLimits(max_leverage=1.0),
    )
    assert tight.ok and "capped by" in tight.detail, tight.detail

    counted: dict[str, int] = {}
    NtxStrategy._count_size_caps(SimpleNamespace(size_caps=counted), tight)
    assert counted, "the shrink must be counted"
    assert any("leverage" in k or "asset" in k for k in counted), counted


# ------------------------------------------------------------------ F052 --- #
def test_a_delivered_critical_is_not_a_failed_alerter_run(tmp_path, monkeypatch):
    """Seen on the VPS 2026-09-20: `ntx-alert.service` sat in `failed` and its
    OnFailure= hook fired another alert about the alerter itself.

    Cause: main() exited 1 whenever any critical alert existed. But systemd's
    notion of failure is "this job did not do its work" -- and delivering a
    critical IS the job. Only a delivery failure is a failed run; the incident
    itself travels by push and by the dead-man switch.
    """
    from ntx.obs import alerts as mod

    posted: list = []
    monkeypatch.setattr(mod, "_post", lambda url, data, headers=None: posted.append(url))
    _snapshot(tmp_path, bot="S003-demo", age_s=5, positions_unprotected=3)
    settings = SimpleNamespace(
        data_dir=tmp_path,
        alert_ntfy_url="https://ntfy.sh/t",
        healthcheck_url="https://hc-ping.com/u",
        alert_bots="S003-demo",
        alert_fills=True,
    )
    monkeypatch.setattr("ntx.config.settings.load", lambda: settings)

    assert mod.main([]) == 0, "a delivered critical is a successful run"
    assert any("/fail" in u for u in posted), "the dead-man switch still reports unhealthy"


def test_an_undelivered_alert_is_still_a_failed_run(tmp_path, monkeypatch):
    from ntx.obs import alerts as mod

    monkeypatch.setattr(mod, "_post", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    _snapshot(tmp_path, bot="S003-demo", age_s=5, positions_unprotected=1)
    settings = SimpleNamespace(
        data_dir=tmp_path,
        alert_ntfy_url="https://ntfy.sh/t",
        healthcheck_url="",
        alert_bots="S003-demo",
        alert_fills=True,
    )
    monkeypatch.setattr("ntx.config.settings.load", lambda: settings)
    assert mod.main([]) != 0
