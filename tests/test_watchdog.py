"""Protection watchdog (R15): no position may exist without a live venue stop.

F007 means stops are attached AFTER the fill, so there is always a window -- and
a stop can also be cancelled, expired or rejected later without the strategy
noticing. The watchdog re-checks periodically. Written before the code (R41).
"""

from __future__ import annotations

import pytest

from ntx.strategies.watchdog import needs_protection


def test_position_with_live_stop_is_fine():
    assert needs_protection(["SOL"], {"SOL": "stop-1"}, lambda oid: True) == []


def test_position_with_no_stop_record_needs_one():
    assert needs_protection(["SOL"], {}, lambda oid: True) == ["SOL"]


def test_position_whose_stop_was_cancelled_needs_one():
    """The stop exists in our bookkeeping but is closed at the venue -- the most
    dangerous case, because our records say we are protected."""
    assert needs_protection(["SOL"], {"SOL": "stop-1"}, lambda oid: False) == ["SOL"]


def test_every_position_is_checked_not_just_the_first():
    """R23: handlers iterate all items per pass."""
    open_ = {"stop-a": True, "stop-b": False}
    result = needs_protection(
        ["A", "B", "C"], {"A": "stop-a", "B": "stop-b"}, lambda oid: open_.get(oid, False)
    )
    assert result == ["B", "C"]


def test_flat_book_needs_nothing():
    assert needs_protection([], {"X": "stop"}, lambda oid: True) == []


def test_status_lookup_failure_counts_as_unprotected():
    """Fail closed: if we cannot confirm a stop is live, assume it is not."""

    def boom(oid):
        raise RuntimeError("cache unavailable")

    assert needs_protection(["SOL"], {"SOL": "stop-1"}, boom) == ["SOL"]


def test_strategy_arms_the_watchdog_timer():
    import inspect

    from ntx.strategies.base import NtxStrategy

    src = inspect.getsource(NtxStrategy.on_start)
    assert "set_timer" in src, "the watchdog must actually be scheduled"
    assert "_protection_watchdog" in src


def test_watchdog_interval_is_configurable_and_defaults_to_20_minutes():
    from ntx.strategies.base import NtxStrategyConfig

    assert NtxStrategyConfig(instrument_ids=["X.OKX"]).watchdog_minutes == 20


@pytest.mark.parametrize(
    ("filled", "covered", "expected"),
    [
        (1.0, 0.0, 1.0),
        (3.0, 1.0, 3.0),
        (3.0, 3.0, None),
        (3.0, 5.0, None),
        (0.0, 0.0, None),
        (0.0, -1.0, None),  # nothing filled, however odd the coverage
        (-1.0, -2.0, None),  # an implausible negative fill never yields a stop
    ],
)
def test_stop_topup_needed(filled, covered, expected):
    """F030: after each entry fill the stop must cover everything filled."""
    from ntx.strategies.watchdog import stop_topup_needed

    assert stop_topup_needed(filled, covered) == expected


M = 60_000_000_000


@pytest.mark.parametrize(
    ("last", "now", "stalled"),
    [
        (None, 10 * M, False),  # nothing seen yet: history/warm-up still running
        (0, 2 * M, False),  # within two intervals + grace
        (0, 3 * M, False),  # exactly at the limit
        (0, 3 * M + 1, True),
        (5 * M, 4 * M, False),  # clock skew never alarms
    ],
)
def test_data_stalled(last, now, stalled):
    """Rehearsal #6 traded nothing for 7 minutes with no log line to say why.
    A live strategy must notice when its bars stop arriving."""
    from ntx.strategies.watchdog import data_stalled

    assert data_stalled(last, now, interval_ns=M, grace_ns=M) is stalled


def test_watchdog_checks_for_data_stalls():
    import inspect

    from ntx.strategies.base import NtxStrategy

    assert "data_stalled" in inspect.getsource(NtxStrategy._protection_watchdog)
    assert "_last_live_bar_ns" in inspect.getsource(NtxStrategy.on_bar)
