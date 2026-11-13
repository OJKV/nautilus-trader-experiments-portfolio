"""Codex review (docs/codex/code_review.md), 2026-09-17 -- reproductions.

Every finding confirmed as a real defect is pinned by a test that FAILS on the
current code (R41). They are marked `xfail(strict=True)` so the suite (and the
commit hook) stays green while the bug is open; the moment a fix lands the test
XPASSes, strict mode turns that into a failure, and the marker must be removed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ntx.strategies.base import NtxStrategy
from ntx.strategies.exits import PositionState
from ntx.strategies.signal import Direction, ExitReason

ROOT = Path(__file__).resolve().parents[1]
# All ten findings are fixed; the marker stays for the next review cycle.
open_bug = pytest.mark.xfail(strict=True, reason="review finding, not fixed yet")


# ---------------------------------------------------------------- C-01 ---- #
def _exit_fake(tmp_path, qty_left=1.0):
    """A strategy with one open, protected LONG and a real journal row."""
    from ntx.obs.journal import Journal

    calls = SimpleNamespace(submitted=[], cancelled=[])
    j = Journal(tmp_path / "j.db")
    j.open_trade(trade_id="t1", correlation_id="c", strategy_id="S", venue="OKX",
                 instrument_id="X", side="LONG", qty=1.0, entry_price=100.0)  # fmt: skip
    st = PositionState(
        Direction.LONG, 100.0, 95.0, 10.0, base_units=1.0,
        meta={"trade_id": "t1", "entry_order_id": "entry", "filled_qty": 1.0,
              "covered_qty": 1.0, "qty": "1"},
    )  # fmt: skip
    live = SimpleNamespace(left=qty_left)

    def market(instrument_id, order_side, quantity, time_in_force=None, reduce_only=False):
        o = SimpleNamespace(
            client_order_id=f"close-{len(calls.submitted) + 1}",
            side=order_side,
            quantity=quantity,
            reduce_only=reduce_only,
        )
        return o

    fake = SimpleNamespace(
        states={"X": st},
        stop_orders={"X": SimpleNamespace(client_order_id="stop-1")},
        stop_client_order_ids={"stop-1": "X"},
        exit_counts={},
        journal=j,
        config=SimpleNamespace(trading_enabled=True),
        close_failures=0,
        _close_orders={},
        _closing={},
        instruments={"X": SimpleNamespace(make_qty=lambda q: q)},
        order_factory=SimpleNamespace(market=market),
        submit_order=lambda o, params=None: calls.submitted.append(o),
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: (
                [SimpleNamespace(quantity=live.left)] if live.left else []
            )
        ),
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        _cancel_protective_stop=lambda inst: calls.cancelled.append(inst),
        _last_close=lambda inst: 99.0,
        _equity_usd=lambda: 100.0,
        _fee=NtxStrategy._fee,
        _entry_px=NtxStrategy._entry_px,
        _pnl=NtxStrategy._pnl,
    )
    for name in (
        "_exit", "_submit_close", "_venue_close_qty", "_position_qty", "_record_close_fill",
        "_close_confirmed", "_close_refused", "_close_journal_trade", "_handle_refused",
        "_venue_writes_allowed",
    ):  # fmt: skip
        setattr(fake, name, getattr(NtxStrategy, name).__get__(fake, NtxStrategy))
    return fake, calls, j, live


def _fill(oid, qty, px, fee=0.05):
    return SimpleNamespace(
        client_order_id=oid, instrument_id="X", last_qty=qty, last_px=px,
        ts_event=1, commission=fee, order_side="SELL",
    )  # fmt: skip


def test_c01_exit_keeps_state_and_stop_until_the_close_is_confirmed(tmp_path):
    """The old code popped the state and cancelled the stop immediately, so a
    failed close left a naked position the watchdog could not see."""
    fake, calls, _j, _live = _exit_fake(tmp_path)
    fake._exit("X", ExitReason.TIME_STOP)
    assert calls.cancelled == [], "stop must stay until the venue confirms flat"
    assert "X" in fake.states and fake.states["X"].meta["closing"] == "time_stop"
    order = calls.submitted[-1]
    assert order.reduce_only is True and fake._close_orders[order.client_order_id] == "X"


def test_c01_rejected_close_keeps_the_position_protected(tmp_path):
    fake, calls, _j, _live = _exit_fake(tmp_path)
    fake._exit("X", ExitReason.TIME_STOP)
    oid = calls.submitted[-1].client_order_id
    fake._handle_refused(
        SimpleNamespace(client_order_id=oid, instrument_id="X", reason="no"), "rejected"
    )
    assert "X" in fake.states and calls.cancelled == []
    assert fake.close_failures == 1
    assert "X" in fake.stop_orders, "protection must remain"


def test_c01_close_is_retried_then_halted_loudly(tmp_path):
    fake, calls, j, _live = _exit_fake(tmp_path)
    fake._exit("X", ExitReason.TIME_STOP)
    for _ in range(3):
        oid = calls.submitted[-1].client_order_id
        fake._handle_refused(
            SimpleNamespace(client_order_id=oid, instrument_id="X", reason="no"), "rejected"
        )
        if not fake.states["X"].meta.get("close_halted"):
            fake._submit_close("X", fake.states["X"])
    assert fake.states["X"].meta["close_halted"] is True
    assert j.count_events("close_refused", within_seconds=60) >= 3


def test_c01_confirmed_close_finalises_state_stop_and_journal(tmp_path):
    fake, calls, j, live = _exit_fake(tmp_path)
    fake._exit("X", ExitReason.TIME_STOP)
    oid = calls.submitted[-1].client_order_id
    fake._record_close_fill(_fill(oid, 0.4, 101.0))  # partial: still open
    assert "X" in fake.states and calls.cancelled == []
    live.left = 0.0
    fake._record_close_fill(_fill(oid, 0.6, 106.0))
    assert "X" not in fake.states and calls.cancelled == ["X"]
    row = j.closed_trades()[0]
    assert row["exit_reason"] == "time_stop"
    assert row["exit_price"] == pytest.approx(104.0)  # vwap 101x0.4 + 106x0.6
    assert row["fees_usd"] == pytest.approx(0.10)


def test_c01_watchdog_resubmits_an_unfinished_close():
    import inspect

    src = inspect.getsource(NtxStrategy._protection_watchdog)
    assert "_submit_close" in src


# ---------------------------------------------------------------- C-02 ---- #
def test_c02_loss_limits_are_enforced_before_entry():
    """NTX_DAILY_LOSS_LIMIT_EQUITY_PCT / NTX_MAX_DRAWDOWN_EQUITY_PCT are only
    printed by doctor; no code path vetoes an entry."""
    from ntx.risk.guards import entry_allowed  # does not exist yet

    ok, why = entry_allowed(
        day_start_equity=100.0, high_water=100.0, equity=94.0, daily_loss_pct=5.0, max_dd_pct=20.0
    )
    assert not ok and "daily" in why


def test_c02_node_passes_loss_limits_to_the_strategy(tmp_path):
    from test_live_node import _settings

    from ntx.live import node

    s = _settings(tmp_path)
    kw = node.strategy_kwargs(node.plan(s), s)
    assert kw["daily_loss_limit_equity_pct"] == s.daily_loss_limit_equity_pct
    assert kw["max_drawdown_equity_pct"] == s.max_drawdown_equity_pct


# ---------------------------------------------------------------- C-03 ---- #
def test_c03_source_data_package_is_not_gitignored():
    """`.gitignore` has an unanchored `data/` that also ignores src/ntx/data/:
    the first commit would ship without the data pipeline, and live preflight
    imports ntx.data.instruments."""
    r = subprocess.run(
        ["git", "check-ignore", "-q", "src/ntx/data/__init__.py"], cwd=ROOT, check=False
    )
    assert r.returncode == 1, "src/ntx/data is ignored by git"


# ---------------------------------------------------------------- H-01 ---- #
def _vs(monkeypatch, pending, post_result):
    from ntx.ops import venue_stops

    sent = []

    def get(base, path, creds, demo):
        kind = "oco" if "ordType=oco" in path else "conditional"
        return {"code": "0", "data": pending.get(kind, [])}

    def post(base, path, body, creds, demo):
        sent.append(body)
        return post_result

    monkeypatch.setattr(venue_stops, "_signed_get", get)
    monkeypatch.setattr(venue_stops, "_signed_post", post)
    return venue_stops, sent


OUR_STOP = {"algoId": "sl1", "slTriggerPx": "95", "closeFraction": "1", "reduceOnly": "true"}
FOREIGN_TP = {"algoId": "tp1", "tpTriggerPx": "120", "slTriggerPx": "", "closeFraction": "",
              "reduceOnly": "true"}  # fmt: skip


def test_h01_unrelated_trigger_order_aborts_instead_of_cancelling_broadly(monkeypatch, tmp_path):
    """The old code cancelled EVERY conditional order on the instrument."""
    from test_live_node import _settings

    vs, sent = _vs(monkeypatch, {"conditional": [OUR_STOP, FOREIGN_TP]}, {"code": "0"})
    with pytest.raises(RuntimeError, match="not our close-position stop"):
        vs.release_stop("SOL-USDT-SWAP.OKX", _settings(tmp_path), True)
    assert sent == [], "nothing may be cancelled when the picture is ambiguous"


def test_h01_per_order_cancel_failure_is_detected(monkeypatch, tmp_path):
    """OKX returns per-order `sCode`; a batch can be accepted while one fails."""
    from test_live_node import _settings

    vs, sent = _vs(
        monkeypatch,
        {"conditional": [OUR_STOP]},
        {"code": "0", "data": [{"algoId": "sl1", "sCode": "51000", "sMsg": "failed"}]},
    )
    with pytest.raises(RuntimeError, match="sCode"):
        vs.release_stop("SOL-USDT-SWAP.OKX", _settings(tmp_path), True)
    assert [o["algoId"] for o in sent[0]] == ["sl1"]


def test_h01_happy_path_returns_the_trigger_price(monkeypatch, tmp_path):
    from test_live_node import _settings

    from ntx.ops import venue_stops

    calls = {"n": 0}

    def get(base, path, creds, demo):
        if "ordType=oco" in path:
            return {"code": "0", "data": []}
        calls["n"] += 1
        return {"code": "0", "data": [OUR_STOP] if calls["n"] == 1 else []}

    monkeypatch.setattr(venue_stops, "_signed_get", get)
    monkeypatch.setattr(
        venue_stops,
        "_signed_post",
        lambda *a, **k: {"code": "0", "data": [{"algoId": "sl1", "sCode": "0"}]},
    )
    assert venue_stops.release_stop("SOL-USDT-SWAP.OKX", _settings(tmp_path), True) == 95.0


def test_h01_stop_still_pending_after_cancel_reports_and_proceeds(monkeypatch, tmp_path):
    """AMENDED 2026-09-24 (L2-05). This used to assert that a stop still listed
    after an accepted cancel raises. It must not.

    H-01's real point stands and is still tested above and in
    tests/test_release_stop_l2_05.py: never guess which order to cancel. But that
    is a question about the venue state BEFORE the cancel. Once the cancel has
    been accepted the position is unprotected, the caller re-places the stop on
    the next line, and raising in between aborts adoption with nothing guarding a
    live leveraged position -- the node exits and systemd waits 120 s.

    If the venue meant what it listed, OKX rejects the replacement (one
    close-position stop per position, F036): loud, counted, repairable."""
    from test_live_node import _settings

    vs, _sent = _vs(
        monkeypatch, {"conditional": [OUR_STOP]}, {"code": "0", "data": [{"sCode": "0"}]}
    )
    seen: list[str] = []
    price = vs.release_stop(
        "SOL-USDT-SWAP.OKX", _settings(tmp_path), True, on_ambiguity=seen.append
    )
    assert price == 95.0
    assert seen, "a leftover stop must be reported, not swallowed"


# ---------------------------------------------------------------- H-02 ---- #
def test_h02_live_node_refuses_skip_preflight(monkeypatch, tmp_path):
    import nautilus_trader.live.node as nlive
    from test_live_node import _settings

    import ntx.config.geo as geo
    import ntx.config.settings as settings_mod
    from ntx.live import node

    monkeypatch.setattr(settings_mod, "load", lambda: _settings(tmp_path))
    monkeypatch.setattr(geo, "check_egress", lambda *a, **k: SimpleNamespace(allowed=True))

    def must_not_connect(*a, **k):
        raise AssertionError("reached TradingNode with preflight skipped on LIVE")

    monkeypatch.setattr(nlive, "TradingNode", must_not_connect)
    assert node.main(["--skip-preflight"]) != 0


# ---------------------------------------------------------------- H-03 ---- #
def test_h03_cancelled_unfilled_entry_leaves_no_phantom(tmp_path):
    from ntx.obs.journal import Journal

    j = Journal(tmp_path / "j.db")
    j.open_trade(trade_id="t1", correlation_id="c", strategy_id="S", venue="OKX",
                 instrument_id="X", side="LONG", qty=1.0, entry_price=100.0,
                 state="submitted")  # fmt: skip
    st = PositionState(
        Direction.LONG, 100.0, 95.0, 10.0,
        meta={"trade_id": "t1", "entry_order_id": "entry", "filled_qty": 0.0},
    )  # fmt: skip
    fake = SimpleNamespace(
        states={"X": st},
        stop_orders={},
        stop_client_order_ids={},
        journal=j,
        unprotected_events=0,
        log=SimpleNamespace(info=print, warning=print, error=print),
        _has_position=lambda inst: False,
        _submit_stop=lambda *a: None,
    )
    fake._entry_canceled = lambda ev: NtxStrategy._entry_canceled(fake, ev)
    NtxStrategy.on_order_canceled(fake, SimpleNamespace(client_order_id="entry", instrument_id="X"))
    assert "X" not in fake.states
    assert j.trade_state("t1") == "cancelled"


# ---------------------------------------------------------------- M-01 ---- #
def test_m01_metrics_use_the_equity_curve_without_closed_trades():
    from ntx.backtest.metrics import compute

    m = compute(pd.DataFrame(), pd.Series([100.0, 50.0, 120.0]), 100.0, 3)
    assert m.max_drawdown_pct == pytest.approx(50.0)
    assert m.return_pct == pytest.approx(20.0)


# ---------------------------------------------------------------- M-02 ---- #
def test_m02_fingerprint_changes_when_content_changes(tmp_path):
    from ntx.backtest.manifest import data_fingerprint

    f = tmp_path / "catalog" / "data" / "bar" / "x" / "part.parquet"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"AAAA")
    a = data_fingerprint(tmp_path)["hash"]
    f.write_bytes(b"BBBB")
    assert data_fingerprint(tmp_path)["hash"] != a


# ---------------------------------------------------------------- M-03 ---- #
@pytest.mark.parametrize("target", ["backtest", "sweep", "paper", "live"])
def test_m03_documented_make_targets_do_something(target):
    r = subprocess.run(["make", "-n", target], cwd=ROOT, capture_output=True, text=True)
    assert "Nothing to be done" not in (r.stdout + r.stderr), f"`make {target}` is a no-op"


# ---------------------------------------------------------------- M-04 ---- #
def test_m04_doctor_fails_on_readable_secret_file(tmp_path):
    from ntx.doctor import secret_file_mode_ok

    f = tmp_path / ".env"
    f.write_text("X=1")
    f.chmod(0o644)
    assert secret_file_mode_ok(f) is False
    f.chmod(0o600)
    assert secret_file_mode_ok(f) is True


# ---------------------------------------------------------------- L-01 ---- #
def test_l01_readme_describes_the_current_system():
    text = (ROOT / "README.md").read_text()
    assert "implementation not started" not in text
    assert "**1.230.0**" in text, "the pinned version must be the one named"
    # 1.231.0 may only appear while explaining why it is NOT used
    for line in text.splitlines():
        if "1.231.0" in line:
            assert "pinned" in line or "no macOS wheel" in line, line
    assert "S003" in text and "Donchian" in text
    assert "S001" not in text.split("## Honest expectations")[0].split("Rejected")[0]


# ------------------------------------------------- C-02 guard behaviour ---- #
def test_c02_daily_loss_pauses_and_drawdown_kills():
    from ntx.risk.guards import Verdict, loss_state

    base = dict(day_start_equity=100.0, high_water=100.0, daily_loss_pct=5.0, max_dd_pct=20.0)
    assert loss_state(equity=96.0, **base)[0] is Verdict.OK
    assert loss_state(equity=95.0, **base)[0] is Verdict.PAUSE
    assert loss_state(equity=80.0, **base)[0] is Verdict.KILL
    assert loss_state(equity=-1.0, **base)[0] is Verdict.KILL
    # a drawdown from an older high-water mark kills even if today is flat
    assert loss_state(equity=79.0, day_start_equity=79.0, high_water=100.0,
                      daily_loss_pct=5.0, max_dd_pct=20.0)[0] is Verdict.KILL  # fmt: skip


def test_c02_limits_off_when_zero():
    from ntx.risk.guards import Verdict, loss_state

    assert loss_state(equity=1.0, day_start_equity=100.0, high_water=100.0,
                      daily_loss_pct=0.0, max_dd_pct=0.0)[0] is Verdict.OK  # fmt: skip


def test_c02_strategy_blocks_entries_when_paused(tmp_path):
    """The gate must be in the entry path, not just a helper."""
    import inspect

    src = inspect.getsource(NtxStrategy._enter)
    assert "_guard_blocks_entry" in src
