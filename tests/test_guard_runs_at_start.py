"""A node that restarts already in KILL territory must not trade for 20 minutes.

Measured on the box, 2026-09-24 guard drill: a node was restarted with a
high-water mark of $1,000 seeded from the journal and equity of $700 -- a 30%
drawdown against a 20% KILL threshold -- holding a real open position. It did
not kill, did not flatten, did not latch. Three minutes later it was still
trading normally.

Nothing was broken in the guard itself. `_risk_check` is wired to the protection
watchdog, and that timer fires every `watchdog_minutes` -- twenty. So the guards
were simply not evaluated yet, and would not have been for another seventeen
minutes, during which a 4h bar close could have opened new positions.

`_seed_risk_state()` restores the high-water mark at start-up (R55/B-04). The
judgement that uses it has to happen there too, not only on the next tick.

Written before the fix (R41).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy, Verdict


def test_a_seeded_drawdown_is_judged_before_the_first_watchdog_tick():
    """The ordering that matters: seed the mark, then judge it, inside on_start."""
    src = inspect.getsource(NtxStrategy.on_start)
    assert "_seed_risk_state" in src
    assert "_risk_check" in src, (
        "on_start seeds the high-water mark but never judges it, so a node that "
        "restarts into a KILL-level drawdown keeps trading until the watchdog "
        "ticks -- up to watchdog_minutes later"
    )
    assert src.index("_seed_risk_state") < src.index("_risk_check"), (
        "judging before seeding compares against an empty mark"
    )


def test_the_judgement_happens_after_adoption_so_it_can_close_what_it_finds():
    """A KILL must be able to flatten the positions adoption just took over."""
    src = inspect.getsource(NtxStrategy.on_start)
    assert src.index("_adopt_positions") < src.index("_risk_check")


def test_the_drill_numbers_do_produce_a_kill():
    """HWM $1,000, equity $700, 20% limit -- the values the drill used."""
    import datetime as dt

    fake = SimpleNamespace(
        _high_water=1000.0,
        _day_start_equity=1000.0,
        _day=dt.datetime.now(dt.UTC).strftime("%Y-%m-%d"),
        _killed=False,
        clock=SimpleNamespace(utc_now=lambda: dt.datetime.now(dt.UTC)),
        config=SimpleNamespace(max_drawdown_equity_pct=20.0, daily_loss_limit_equity_pct=100.0),
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: None,
            error=lambda *a: None,
            debug=lambda *a: None,
        ),
        _equity_or_none=lambda: 700.0,
    )
    for n in ("_loss_verdict",):
        setattr(fake, n, getattr(NtxStrategy, n).__get__(fake, NtxStrategy))
    verdict, why = fake._loss_verdict()
    assert verdict is Verdict.KILL, f"expected KILL, got {verdict}: {why}"


def test_the_guard_is_still_on_the_watchdog_timer():
    """Start-up is an addition, not a replacement: a drawdown that develops
    while the node runs must still be caught (B-04)."""
    assert "_risk_check" in inspect.getsource(NtxStrategy._protection_watchdog)
