"""N3-03 / T3-03: the HWM implausibility check was skipped when equity was unknown at start.

`_seed_risk_state` guarded with `if equity_now and hwm > equity_now * 5`: when the
venue equity hook had not answered yet (None), ANY stored mark was accepted, and
the first equity reading then judged the account against it -- N2-01's false
KILL, reachable again through a slow first REST call. Now an unchecked mark is
held back and checked against the first equity actually read.

Written before the fix (R41).
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from test_risk_state import _fake

from ntx.risk.guards import Verdict
from ntx.strategies.base import NtxStrategy


def _judge(fake):
    fake.clock = SimpleNamespace(utc_now=lambda: dt.datetime(2026, 10, 6))
    fake._loss_verdict = NtxStrategy._loss_verdict.__get__(fake, NtxStrategy)
    return fake._loss_verdict()


def test_an_implausible_mark_seeded_while_equity_is_unknown_is_still_refused(tmp_path):
    _f, j = _fake(tmp_path)
    j.record_equity(5101.70)
    equity = {"now": None}
    fresh, _ = _fake(tmp_path, _equity_or_none=lambda: equity["now"])
    fresh.journal = j
    events: list = []
    fresh._jevent = lambda kind, **kw: events.append(kind)
    fresh._seed_risk_state()
    equity["now"] = 123.45  # the venue answers on the next call
    verdict, why = _judge(fresh)
    assert verdict is not Verdict.KILL, f"unchecked seed KILLed a healthy account: {why}"
    assert fresh._high_water <= 123.45 * 5
    assert "hwm_implausible" in events


def test_a_plausible_mark_seeded_while_equity_is_unknown_still_counts(tmp_path):
    """Deferring the check must not drop the mark (R55): 50 -> 37 is a real 26% DD."""
    _f, j = _fake(tmp_path)
    j.record_equity(50.0)
    equity = {"now": None}
    fresh, _ = _fake(tmp_path, _equity_or_none=lambda: equity["now"])
    fresh.journal = j
    fresh._seed_risk_state()
    assert _judge(fresh)[0] is Verdict.PAUSE, "unknown equity pauses, never kills"
    equity["now"] = 37.0
    verdict, _why = _judge(fresh)
    assert verdict is Verdict.KILL
    assert fresh._high_water == 50.0
