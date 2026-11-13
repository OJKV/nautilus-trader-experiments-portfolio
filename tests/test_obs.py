"""Tests for atomic writes, the trade journal, and economic health checks.

Each test maps to a numbered failure pattern in docs/13-production-hardening.md
or a standing rule in CLAUDE.md, named in the docstring.
"""

from __future__ import annotations

import json

import pytest

from ntx.obs.atomic import atomic_write_json, atomic_write_text, safe_read_json
from ntx.obs.health import Severity, Snapshot, Thresholds, evaluate, worst_severity
from ntx.obs.journal import TERMINAL_STATES, TRADE_STATES, InvalidTransition, Journal


# ------------------------------- atomic writes ------------------------------ #
def test_atomic_write_roundtrip(tmp_path):
    """Pattern 3: state files must be written atomically."""
    p = tmp_path / "state.json"
    atomic_write_json(p, {"equity": 37.0, "positions": []})
    assert safe_read_json(p) == {"equity": 37.0, "positions": []}


def test_atomic_write_leaves_no_temp_files(tmp_path):
    p = tmp_path / "state.json"
    atomic_write_json(p, {"a": 1})
    assert [f.name for f in tmp_path.iterdir()] == ["state.json"]


def test_failed_write_leaves_previous_state_intact(tmp_path):
    """A write that blows up must not destroy the last good state, nor leave a
    temp file behind. Uses a circular reference, which json cannot encode even
    with `default=str`."""
    p = tmp_path / "state.json"
    atomic_write_json(p, {"good": True})

    circular: dict = {}
    circular["self"] = circular

    with pytest.raises(ValueError):
        atomic_write_json(p, circular)
    assert safe_read_json(p) == {"good": True}
    assert [f.name for f in tmp_path.iterdir()] == ["state.json"]


def test_default_str_encodes_datetimes_and_paths(tmp_path):
    """`default=str` is deliberate: timestamps and paths appear in state files."""
    from datetime import UTC, datetime

    p = tmp_path / "state.json"
    atomic_write_json(p, {"ts": datetime(2026, 9, 15, tzinfo=UTC), "dir": tmp_path})
    loaded = safe_read_json(p)
    assert loaded["ts"].startswith("2026-09-15")
    assert str(tmp_path) == loaded["dir"]


def test_corrupt_file_is_deleted_not_retried_forever(tmp_path):
    """Pattern 3: a reader must never loop on a broken state file."""
    p = tmp_path / "state.json"
    p.write_text('{"price": 2274.0, "trigger": "path1_met')  # truncated mid-flush
    assert safe_read_json(p) is None
    assert not p.exists(), "corrupt file must be removed so the pipeline unblocks"


def test_missing_file_returns_none(tmp_path):
    assert safe_read_json(tmp_path / "nope.json") is None


def test_validate_rejects_invalid_json_before_commit(tmp_path):
    p = tmp_path / "x.json"
    with pytest.raises(json.JSONDecodeError):
        atomic_write_text(p, "{not json", validate=True)
    assert not p.exists()


# --------------------------------- journal ---------------------------------- #
@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path / "journal.db")


def test_every_state_reaches_a_terminal_state():
    """Pattern 4 / P-004: 246 ghost positions blocked trading for months because
    the state machine had no terminal transitions."""
    assert {"closed", "rejected", "cancelled"} == TERMINAL_STATES
    for state in TRADE_STATES:
        seen, frontier = set(), [state]
        while frontier:
            cur = frontier.pop()
            if cur in seen:
                continue
            seen.add(cur)
            frontier.extend(TRADE_STATES[cur])
        assert seen & TERMINAL_STATES, f"{state} cannot reach a terminal state"


def test_trade_lifecycle(journal: Journal):
    journal.open_trade(
        trade_id="T1",
        correlation_id="c-1",
        strategy_id="S001",
        venue="OKX",
        instrument_id="SOL-USDT-SWAP.OKX",
        side="LONG",
        qty=0.1,
        entry_price=100.0,
        notional_usd=10.0,
        risk_usd=0.37,
        stop_price=96.0,
    )
    assert len(journal.open_trades()) == 1

    journal.transition("T1", "closing")
    journal.transition(
        "T1",
        "closed",
        exit_price=104.0,
        exit_reason="tsl",
        pnl_net_usd=0.35,
        fees_usd=0.01,
        funding_usd=0.0,
    )

    assert journal.open_trades() == []
    closed = journal.closed_trades()
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "tsl"
    assert closed[0]["closed_at"] is not None


def test_illegal_transition_is_rejected(journal: Journal):
    journal.open_trade(
        trade_id="T2",
        correlation_id="c",
        strategy_id="S001",
        venue="OKX",
        instrument_id="X.OKX",
        side="LONG",
    )
    journal.transition("T2", "closed")
    with pytest.raises(InvalidTransition):
        journal.transition("T2", "open")  # terminal states are terminal


def test_unknown_trade_raises(journal: Journal):
    with pytest.raises(KeyError):
        journal.transition("nope", "closed")


def test_attempts_recorded_before_outcome(journal: Journal):
    """R19 / pattern 7: cooldowns key on attempts, not successes."""
    journal.record_attempt(
        attempt_id="a1",
        strategy_id="S001",
        instrument_id="SOL-USDT-SWAP.OKX",
        side="LONG",
        intended_qty=0.1,
        intended_notional=10.0,
    )
    assert journal.seconds_since_last_attempt() is not None
    assert journal.seconds_since_last_attempt("SOL-USDT-SWAP.OKX") is not None

    # A rejected attempt still counts toward the cooldown -- that is the point.
    journal.resolve_attempt("a1", "rejected", "min notional")
    assert journal.seconds_since_last_attempt() is not None


def test_no_attempt_yet_returns_none(journal: Journal):
    assert journal.seconds_since_last_attempt() is None


def test_events_and_counting(journal: Journal):
    for _ in range(6):
        journal.event("order_rejected", severity="high", reason="min notional")
    assert journal.count_events("order_rejected", within_seconds=300) == 6
    assert journal.count_events("kill_switch", within_seconds=300) == 0


def test_equity_and_high_water_mark(journal: Journal):
    assert journal.equity_high_water_mark() is None
    journal.record_equity(37.0)
    journal.record_equity(42.0, unrealised=1.0, open_positions=2)
    assert journal.equity_high_water_mark() == 42.0


def test_journal_survives_reopen(tmp_path):
    """The whole reason this exists: nautilus reports vanish on restart."""
    path = tmp_path / "j.db"
    j1 = Journal(path)
    j1.open_trade(
        trade_id="T",
        correlation_id="c",
        strategy_id="S001",
        venue="OKX",
        instrument_id="X.OKX",
        side="LONG",
    )
    j1.transition("T", "closing")
    j1.transition("T", "closed", pnl_net_usd=1.23)

    j2 = Journal(path)  # simulates a process restart
    assert len(j2.closed_trades()) == 1
    assert j2.closed_trades()[0]["pnl_net_usd"] == 1.23


def test_hours_since_last_closed_trade(journal: Journal):
    assert journal.hours_since_last_closed_trade() is None
    journal.open_trade(
        trade_id="T",
        correlation_id="c",
        strategy_id="S001",
        venue="OKX",
        instrument_id="X.OKX",
        side="LONG",
    )
    journal.transition("T", "closing")
    journal.transition("T", "closed")
    assert journal.hours_since_last_closed_trade() == pytest.approx(0, abs=0.01)


# ------------------------------ health checks ------------------------------- #
def test_unprotected_position_is_critical():
    """R15: a position must never exist without a venue-side stop."""
    checks = evaluate(Snapshot(positions_unprotected=1, universe_size=3))
    c = next(c for c in checks if c.name == "positions_unprotected")
    assert c.severity is Severity.CRITICAL
    assert worst_severity(checks) is Severity.CRITICAL


def test_all_protected_is_ok():
    checks = evaluate(
        Snapshot(
            positions_open=2, positions_unprotected=0, universe_size=3, hours_since_closed_trade=1.0
        )
    )
    c = next(c for c in checks if c.name == "positions_unprotected")
    assert c.severity is Severity.OK


def test_idle_bot_is_flagged():
    """Pattern 15: running is not trading."""
    checks = evaluate(Snapshot(universe_size=3, hours_since_closed_trade=100.0))
    c = next(c for c in checks if c.name == "activity")
    assert c.severity is Severity.HIGH
    assert "idle" in c.message


def test_paused_bot_is_not_flagged_as_idle():
    checks = evaluate(Snapshot(universe_size=3, hours_since_closed_trade=500.0, paused=True))
    assert next(c for c in checks if c.name == "activity").severity is Severity.INFO


def test_empty_universe_is_flagged():
    checks = evaluate(Snapshot(universe_size=0))
    assert next(c for c in checks if c.name == "activity").severity is Severity.HIGH


def test_reject_storm_is_critical():
    checks = evaluate(Snapshot(universe_size=3, rejects_last_5min=5, hours_since_closed_trade=1.0))
    assert next(c for c in checks if c.name == "reject_storm").severity is Severity.CRITICAL


def test_equity_below_min_notional_is_critical():
    """Pattern 13: the small-account analogue of running out of cash."""
    checks = evaluate(
        Snapshot(
            universe_size=1,
            equity_usd=0.50,
            smallest_min_notional_usd=0.74,
            hours_since_closed_trade=1.0,
        )
    )
    assert next(c for c in checks if c.name == "capital").severity is Severity.CRITICAL


def test_equity_with_headroom_is_ok():
    checks = evaluate(
        Snapshot(
            universe_size=1,
            equity_usd=37.0,
            smallest_min_notional_usd=0.74,
            hours_since_closed_trade=1.0,
        )
    )
    assert next(c for c in checks if c.name == "capital").severity is Severity.OK


def test_slippage_drift_detected():
    """If live slippage exceeds the model, every backtest conclusion is suspect."""
    checks = evaluate(
        Snapshot(
            universe_size=1,
            hours_since_closed_trade=1.0,
            realised_slippage_bps=12.0,
            model_slippage_bps=2.0,
        )
    )
    assert next(c for c in checks if c.name == "slippage_drift").severity is Severity.HIGH


def test_fee_drift_detected():
    """Pattern 1: documented fee rates were 6-12x reality."""
    checks = evaluate(
        Snapshot(
            universe_size=1,
            hours_since_closed_trade=1.0,
            realised_fee_rate=0.005,
            configured_fee_rate=0.0005,
        )
    )
    assert next(c for c in checks if c.name == "fee_drift").severity is Severity.HIGH


def test_stale_data_detected():
    checks = evaluate(
        Snapshot(
            universe_size=1,
            hours_since_closed_trade=1.0,
            bar_period_seconds=3600,
            seconds_since_last_bar={"SOL.OKX": 9000, "BTC.OKX": 100},
        )
    )
    c = next(c for c in checks if c.name == "stale_data")
    assert c.severity is Severity.HIGH
    assert "1 instrument" in c.message


def test_healthy_snapshot_has_no_alerts():
    checks = evaluate(
        Snapshot(
            universe_size=3,
            positions_open=2,
            positions_unprotected=0,
            hours_since_closed_trade=2.0,
            equity_usd=37.0,
            smallest_min_notional_usd=0.74,
            bar_period_seconds=3600,
            seconds_since_last_bar={"SOL.OKX": 120},
            realised_slippage_bps=2.5,
            model_slippage_bps=2.0,
            realised_fee_rate=0.0005,
            configured_fee_rate=0.0005,
        )
    )
    assert worst_severity(checks) is Severity.OK, [c for c in checks if c.severity != Severity.OK]


def test_thresholds_are_configurable():
    snap = Snapshot(universe_size=1, hours_since_closed_trade=10.0)
    assert next(c for c in evaluate(snap) if c.name == "activity").severity is Severity.OK
    strict = Thresholds(idle_hours=5.0)
    assert next(c for c in evaluate(snap, strict) if c.name == "activity").severity is Severity.HIGH


def test_pull_journal_hint_uses_the_real_review_flag():
    """The Makefile told the user `--review --journal`; review.py takes `--db`.
    A hint that does not run is a trap during an incident."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    hint = [x for x in (root / "Makefile").read_text().splitlines() if "obs.review" in x]
    assert hint, "pull-journal should tell the user how to read what it pulled"
    flags = set(re.findall(r"--[a-z-]+", " ".join(hint)))
    src = (root / "src/ntx/obs/review.py").read_text()
    for f in flags:
        assert f'"{f}"' in src, f"Makefile suggests {f}, review.py does not accept it"


def test_vps_check_knows_about_the_demo_phase():
    """During the demo weeks the live OKX_API_* vars are legitimately empty."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts/vps_check.sh").read_text()
    assert "ntx-soak" in src, "the checker must know the soak unit"
    assert "ntx-alert" in src, "the checker must know the alerter timer"
    assert "demo" in src.lower()
