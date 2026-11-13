"""P2: `on_stop` closed every cache position when `flatten_on_stop` was set.

`close_all_positions` reads the cache alone -- F069's path: a phantom SHORT in
the cache is "closed" by a market BUY that opens a real long (R74). The flag is
set by node assembly only for REHEARSAL_ONLY strategies on demo, but nothing in
the strategy enforced either half, so one wrong config line would carry it to
live. Now: demo only, and every close goes through the venue-checked `_flatten`.

Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy

LINK = "LINK-USDT-SWAP.OKX"


def _quiet(*_a) -> None:
    return None


def _fake(*, is_demo: bool, cached: float, venue: dict):
    closed: list = []
    cancelled: list = []
    events: list = []
    pos = SimpleNamespace(quantity=abs(cached), signed_qty=cached)
    fake = SimpleNamespace(
        _last_equity_ts=-1,
        config=SimpleNamespace(flatten_on_stop=True, is_demo=is_demo),
        instruments={LINK: SimpleNamespace()},
        stop_client_order_ids={},
        stop_orders={},
        entries_attempted=0, entries_submitted=0, rejects={}, size_caps={},
        exit_counts={}, unprotected_events=0,
        cache=SimpleNamespace(positions_open=lambda instrument_id=None: [pos] if cached else []),
        log=SimpleNamespace(info=_quiet, warning=_quiet, error=_quiet, debug=_quiet),
        _jevent=lambda kind, **kw: events.append(kind),
        _venue_writes_allowed=lambda why: True,
        venue_position_hook=lambda: venue,
        cancel_all_orders=lambda inst: cancelled.append(inst),
        close_all_positions=lambda inst: closed.append(("all", inst)),
        close_position=lambda p: closed.append(("one", p.signed_qty)),
    )  # fmt: skip
    for name in ("on_stop", "_flatten", "_venue_agrees_position"):
        setattr(fake, name, getattr(NtxStrategy, name).__get__(fake, NtxStrategy))
    return fake, closed, cancelled, events


def test_a_phantom_is_not_closed_on_stop():
    fake, closed, _c, events = _fake(is_demo=True, cached=-0.4, venue={})
    fake.on_stop()
    assert closed == [], "closing a phantom opens a real position (R74, F069)"
    assert "phantom_flatten_refused" in events


def test_a_real_demo_position_is_still_flattened():
    fake, closed, cancelled, _e = _fake(is_demo=True, cached=-0.4, venue={"LINK-USDT-SWAP": -0.4})
    fake.on_stop()
    assert closed == [("one", -0.4)]
    assert cancelled == [LINK]


def test_never_on_a_live_account():
    fake, closed, cancelled, _e = _fake(is_demo=False, cached=0.4, venue={"LINK-USDT-SWAP": 0.4})
    fake.on_stop()
    assert closed == [] and cancelled == [], "flatten_on_stop is a demo rehearsal tool only"
