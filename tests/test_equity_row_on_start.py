"""The high-water mark must survive a restart, which needs a row to survive into.

Measured on the live account 2026-09-25 after 18h52m and 5 bar closes:

    sqlite3 data/journal.db 'select count(*) from equity'  ->  0

`_append_equity` writes to the journal on every 6th sample, and `equity_curve`
starts empty in every new process. On 4h bars that is one row per 24 hours -- so
a node restarted more often than daily (a deploy, a crash, a reboot) never writes
one at all, and `_seed_risk_state` has nothing to read.

Not dangerous in itself: an absent mark means the guard starts from current
equity, which is the safe direction. But R55 says risk state that gates money
survives a restart, and on the live account it currently does not -- the mark is
whatever the account happened to be worth at the last start.

The first sample of a process is the one worth keeping: it is the anchor every
later restart seeds from. Sparse sampling after that is still right -- a 4h
backtest over six years would otherwise write ~13k rows.
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy


def _fake(*, equity=1000.0):
    written: list[float] = []
    fake = SimpleNamespace(
        equity_curve=[],
        states={},
        journal=SimpleNamespace(
            record_equity=lambda eq, open_positions=0: written.append(eq),
        ),
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: None,
            debug=lambda *a: None,
        ),
        _risk_equity_for_journal=lambda: equity,
    )
    fake._jwrite = lambda _name, fn: fn()
    fake._append_equity = NtxStrategy._append_equity.__get__(fake, NtxStrategy)
    return fake, written


def test_the_first_sample_of_a_process_is_always_journalled():
    """Otherwise a node that restarts more often than once a day never writes
    one, and the drawdown guard has no mark to seed from across a restart."""
    fake, written = _fake()
    fake._append_equity(1, 1000.0)
    assert written == [1000.0], "no equity row after the first sample"


def test_sampling_stays_sparse_after_that():
    """A 4h backtest over six years must not write ~13k rows."""
    fake, written = _fake()
    for i in range(1, 25):
        fake._append_equity(i, 1000.0)
    # 1 (the anchor) + every 6th sample
    assert len(written) < 8, f"too many rows: {len(written)}"
    assert len(written) >= 4, f"stopped sampling: {len(written)}"


def test_the_curve_itself_still_gets_every_point():
    """The in-memory curve drives the mark-to-market metrics (R36) and must not
    be thinned by the journal's sampling."""
    fake, _written = _fake()
    for i in range(1, 13):
        fake._append_equity(i, 1000.0 + i)
    assert len(fake.equity_curve) == 12


def test_a_missing_equity_is_not_recorded_as_zero():
    fake, written = _fake()
    fake._append_equity(1, None)
    assert fake.equity_curve == [] and written == []
