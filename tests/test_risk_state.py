"""Risk state that survives a restart, and the false KILL it caused (N2-01/N2-02).

On 2026-09-19 13:35 UTC the demo soak flattened itself and latched a KILL with
`drawdown 99.3% >= 20.0% of high-water`. Nothing was wrong with the account.

Two different definitions of "equity" met each other:

  journal `equity` table   `portfolio.equity(venue)` summed over EVERY currency
                           -> $5,101.70 on a demo account full of coin dust
  the guard                `_equity_usd()`: the override, or the USDT balance
                           -> $123.45

`_seed_risk_state` read the first and handed it to a guard that compares the
second, so the restart manufactured a 99.3% drawdown. Written before the fix
(R41).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ntx.obs.journal import Journal
from ntx.strategies.base import NtxStrategy


def _fake(tmp_path, equity=123.45, **over):
    j = Journal(tmp_path / "j.db")
    fake = SimpleNamespace(
        journal=j,
        _high_water=0.0,
        _killed=False,
        _day=None,
        _day_start_equity=0.0,
        entry_skips={},
        states={},
        equity_curve=[],
        config=SimpleNamespace(
            state_path=str(tmp_path / "S003-demo.json"),
            is_demo=True,
            daily_loss_limit_equity_pct=5.0,
            max_drawdown_equity_pct=20.0,
            equity_override_usd=None,
        ),
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        _equity_usd=lambda: equity,
        _equity_or_none=lambda: equity,
    )
    fake.__dict__.update(over)
    for name in ("_seed_risk_state", "_kill_latch_path", "_latch_kill", "_jwrite", "_jevent",
                 "_append_equity", "_risk_equity_for_journal"):  # fmt: skip
        setattr(fake, name, getattr(NtxStrategy, name).__get__(fake, NtxStrategy))
    return fake, j


# ------------------------------------------------------------------ N2-01 --- #
def test_the_journal_records_the_same_equity_the_guard_compares(tmp_path):
    """One definition, or a restart invents a drawdown out of nothing."""
    fake, j = _fake(tmp_path, equity=123.45)
    # The in-memory curve is mark-to-market across everything (R36, for metrics);
    # what reaches the journal must be the RISK basis.
    for i in range(6):
        fake._append_equity(i, 5101.70)
    assert j.equity_high_water_mark() == pytest.approx(123.45), "journal must store the guard basis"


def test_a_restart_does_not_manufacture_a_drawdown(tmp_path):
    """The exact 2026-09-19 sequence: record, restart, seed, judge."""
    from ntx.risk.guards import Verdict

    fake, j = _fake(tmp_path, equity=123.45)
    for i in range(6):
        fake._append_equity(i, 5101.70)

    fresh, _ = _fake(tmp_path, equity=123.45)
    fresh.journal = j
    fresh._seed_risk_state()
    fresh.clock = SimpleNamespace(utc_now=lambda: __import__("datetime").datetime(2026, 9, 19))
    fresh._loss_verdict = NtxStrategy._loss_verdict.__get__(fresh, NtxStrategy)
    verdict, why = fresh._loss_verdict()
    assert verdict is not Verdict.KILL, why


def test_an_implausible_seed_is_refused_not_acted_on(tmp_path):
    """Defence in depth: if a stored high-water mark is wildly out of scale with
    the account, the honest reading is "these are different units", not "you lost
    99% of your money". Refuse the seed, say so loudly, keep trading."""
    _fake0, j = _fake(tmp_path, equity=123.45)
    j.record_equity(5101.70)
    fresh, _ = _fake(tmp_path, equity=123.45)
    fresh.journal = j
    fresh._seed_risk_state()
    assert fresh._high_water <= 123.45 * 5, f"seeded {fresh._high_water} against $123.45 of equity"


# ------------------------------------------------------------------ N2-02 --- #
def test_a_latched_kill_blocks_entries(tmp_path):
    """The latch survived a restart but nothing consulted it, so the node came
    back up and traded as if nothing had happened."""
    first, _j = _fake(tmp_path)
    first._latch_kill()
    fresh, _ = _fake(tmp_path)
    fresh._seed_risk_state()
    assert fresh._killed is True
    for name in ("_guard_blocks_entry", "_loss_verdict", "_kill_all"):
        setattr(fresh, name, getattr(NtxStrategy, name).__get__(fresh, NtxStrategy))
    fresh.clock = SimpleNamespace(utc_now=lambda: __import__("datetime").datetime(2026, 9, 19))
    assert fresh._guard_blocks_entry("X") is True
    assert fresh.entry_skips.get("killed") == 1


def test_a_latched_kill_is_visible_in_the_snapshot(tmp_path):
    """A halted bot read green everywhere: the snapshot never carried `killed`,
    so the monitor, the health check and the alerter all said fine (O2-07)."""
    import inspect

    src = inspect.getsource(NtxStrategy.write_state)
    assert '"killed"' in src


def test_a_halted_bot_is_not_healthy():
    import time

    from ntx.ops.snapshot_check import problems

    snap = {
        "ts_ns": int(time.time() * 1e9),
        "is_demo": True,
        "trading_enabled": True,
        "killed": True,
        "positions_unprotected": 0,
        "positions": [],
        "instruments": [{"id": "SOL", "ready": True}],
    }
    assert any("kill" in p.lower() for p in problems(snap, now=time.time(), max_age_s=120))


def test_there_is_a_supported_way_to_clear_a_kill(tmp_path):
    """The log said "delete the file", which re-KILLs 20 minutes later because
    the high-water mark is still above the account. Clearing a KILL has to
    re-base the mark too.

    And re-basing cannot mean "append a lower number": the mark is a MAX over
    the table, so the poisoned row still wins. It has to forget everything
    before the re-base -- without deleting history (R29).
    """
    from ntx.ops.unkill import clear_kill

    first, j = _fake(tmp_path)
    j.record_equity(5101.70)  # the poisoned row that caused the false KILL
    first._latch_kill()
    path = first._kill_latch_path()
    assert path.exists()
    assert j.equity_high_water_mark() == pytest.approx(5101.70)

    clear_kill(path, journal=j, equity=123.45, note="false KILL, unit mismatch")
    assert not path.exists()
    assert j.equity_high_water_mark() == pytest.approx(123.45), "the mark must be re-based"
    # History is kept, just no longer counted towards the mark.
    with j._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM equity").fetchone()[0] >= 2


def test_a_rebase_only_forgets_what_came_before_it(tmp_path):
    _first, j = _fake(tmp_path)
    j.record_equity(5101.70)
    j.rebase_high_water(35.0)
    j.record_equity(45.0)  # the account genuinely grows afterwards
    assert j.equity_high_water_mark() == pytest.approx(45.0)


# ------------------------------------------------------------------ N2-03 --- #
def test_funding_the_account_is_not_a_profit_and_withdrawing_is_not_a_loss(tmp_path):
    """A sample account receives a deposit. A drawdown guard that only knows
    MAX(equity) reads a deposit as a new high (so the next real 20% drawdown is
    measured from money that was never earned) and a withdrawal as a loss (so
    taking profit out can KILL the bot)."""
    from ntx.ops.unkill import rebase_for_transfer

    _f, j = _fake(tmp_path)
    j.record_equity(123.45)
    assert j.equity_high_water_mark() == pytest.approx(123.45)

    rebase_for_transfer(j, equity=200.0, note="deposit")
    assert j.equity_high_water_mark() == pytest.approx(200.0)

    # And after taking money out, the mark follows down rather than pretending
    # the account lost it.
    rebase_for_transfer(j, equity=60.0, note="withdrawal")
    assert j.equity_high_water_mark() == pytest.approx(60.0)


# ------------------------------------------------------------------ T2-01 --- #
def test_opening_a_position_is_not_a_loss(tmp_path):
    """`balance_total(USDT)` is AVAILABLE CASH on OKX, not equity: the isolated
    margin leaves it when a position opens and returns on close, and it does not
    track unrealised PnL at all.

    Measured from our own drill logs: drill A1 opened $8.08 of notional at 3x and
    USDT total fell 4999.75127746 -> 4997.05443826, exactly margin ($2.693) plus
    the 0.05% taker fee; the 09-18 soak repeated one frozen total 319 times while
    a position was open.

    On $123.45 with three positions of ~$3 margin the guard would read ~$28 --
    a 26% "drawdown" against a $123.45 high-water mark. That is KILL, flatten and
    latch, with a perfectly healthy book. So a live node reads the venue's own
    equity figure instead (injected at node assembly, R3).
    """
    from ntx.risk.guards import Verdict

    venue_equity = {"v": 100.0}
    cash = {"v": 100.0}
    fake = SimpleNamespace(
        config=SimpleNamespace(
            equity_override_usd=None,
            daily_loss_limit_equity_pct=5.0,
            max_drawdown_equity_pct=20.0,
            equity_refresh_secs=60,
        ),
        instrument_ids=[SimpleNamespace(venue="OKX")],
        states={},
        equity_hook=lambda: venue_equity["v"],
        _equity_cache=None,
        _equity_cache_ns=0,
        portfolio=SimpleNamespace(
            account=lambda venue: SimpleNamespace(
                balance_total=lambda ccy: SimpleNamespace(as_double=lambda: cash["v"])
            ),
            unrealized_pnls=lambda venue: {},
        ),
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        clock=SimpleNamespace(
            timestamp_ns=lambda: 0,
            utc_now=lambda: __import__("datetime").datetime(2026, 9, 20),
        ),
        _high_water=100.0,
        _day_start_equity=100.0,
        _day="2026-09-20",
        _killed=False,
    )
    for name in ("_equity_or_none", "_equity_usd", "_loss_verdict", "_hooked_equity"):
        setattr(fake, name, getattr(NtxStrategy, name).__get__(fake, NtxStrategy))

    assert fake._equity_or_none() == pytest.approx(100.0)

    # Open $13 of notional at 3x. OKX moves $4.33 of margin out of AVAILABLE
    # cash, but the account's equity is unchanged.
    cash["v"] = 100.0 - 13.0 / 3
    fake._equity_cache = None  # force a re-read
    assert fake._equity_or_none() == pytest.approx(100.0), "committing margin is not a loss"
    verdict, why = fake._loss_verdict()
    assert verdict is Verdict.OK, why


def test_a_stale_venue_equity_read_is_unknown_not_zero():
    """Unknown maps to PAUSE; zero would map to KILL (B-07)."""
    now = {"t": 0}
    fake = SimpleNamespace(
        config=SimpleNamespace(equity_override_usd=None, equity_refresh_secs=60),
        instrument_ids=[SimpleNamespace(venue="OKX")],
        states={},
        equity_hook=lambda: (_ for _ in ()).throw(OSError("okx timeout")),
        _equity_cache=99.0,
        _equity_cache_ns=0,
        log=SimpleNamespace(info=print, warning=print, error=print, debug=print),
        clock=SimpleNamespace(timestamp_ns=lambda: now["t"]),
    )
    for name in ("_equity_or_none", "_hooked_equity"):
        setattr(fake, name, getattr(NtxStrategy, name).__get__(fake, NtxStrategy))

    now["t"] = 90 * 1_000_000_000  # one failed refresh: keep serving the last good value
    assert fake._equity_or_none() == pytest.approx(99.0)
    now["t"] = 600 * 1_000_000_000  # far too old to act on
    assert fake._equity_or_none() is None


def test_a_backtest_reads_the_account_balance_and_nothing_else():
    """No hook, no margin arithmetic: exactly what every recorded gate ran on."""
    fake = SimpleNamespace(
        config=SimpleNamespace(equity_override_usd=None),
        instrument_ids=[SimpleNamespace(venue="OKX")],
        states={},
        equity_hook=None,
        portfolio=SimpleNamespace(
            account=lambda venue: SimpleNamespace(
                balance_total=lambda ccy: SimpleNamespace(as_double=lambda: 10_000.0)
            )
        ),
        log=SimpleNamespace(info=print, warning=print, error=print),
    )
    fake._equity_or_none = NtxStrategy._equity_or_none.__get__(fake, NtxStrategy)
    assert fake._equity_or_none() == pytest.approx(10_000.0)


def test_the_live_node_installs_a_venue_equity_source():
    import inspect

    from ntx.live import node

    assert "equity_hook" in inspect.getsource(node)
