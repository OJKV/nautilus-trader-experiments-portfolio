"""P1 (live restart 2026-10-06 21:14 UTC): an entry on an instrument whose cache holds a phantom.

Startup reconciliation rebuilt ADA as SHORT 0.4 in the cache -- the closing SELL
of the 10-05 trade was inside the 1440-min order lookback, its opening BUY was
not -- while OKX held nothing. Adoption skipped it (`adoption_phantom_skipped`)
and the watchdog flagged it (`phantom_cache_position`), but `_enter` refused
only `_venue_blocked` instruments, not phantom ones.

Under nautilus NETTING every fill of this strategy on ADA lands on ONE position
id, `{instrument}-{strategy}` (engine.pyx `_determine_netting_position_id`), and
the phantom was claimed by this strategy (`external_order_claims`). So a LONG
entry of the same 0.4 nets the phantom to flat: the venue holds a real +0.4,
nautilus emits PositionClosed, and `on_position_closed` -- a filled state that is
not `closing` -- reads it as "closed externally": state dropped, journal row
closed, venue stop CANCELLED. A real position with no stop and no state; the
cache is flat, so the orphan repair never sees it either. A larger entry flips
the cache (same PositionClosed, plus a residual long); a smaller one leaves a
residual short that a later close only grows.

Two fixes, both written as failing tests first (R41):
  1. `_enter` refuses an instrument whose cache holds a position we have no
     state for -- phantom or orphan, entering on top of it is never right.
  2. `on_position_closed` asks the venue before forgetting a position it did
     not close itself (R72): a cache closure is not a venue closure.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ntx.strategies import base as base_mod
from ntx.strategies.base import NtxStrategy
from ntx.strategies.exits import Direction, PositionState

ADA = "ADA-USDT-SWAP.OKX"


def _quiet(*_a) -> None:
    return None


class NettingCache:
    """One signed position per instrument, as nautilus NETTING keeps it."""

    def __init__(self, signed: float = 0.0) -> None:
        self.signed = signed

    def positions_open(self, instrument_id=None, strategy_id=None):
        if not self.signed:
            return []
        return [SimpleNamespace(quantity=abs(self.signed), signed_qty=self.signed)]

    def fill(self, signed_qty: float) -> bool:
        """Apply a fill; True when nautilus would emit PositionClosed."""
        before = self.signed
        self.signed = round(before + signed_qty, 12)
        return bool(before) and (self.signed == 0 or (self.signed > 0) != (before > 0))


def _entry_fake(monkeypatch, cache):
    submitted: list = []
    events: list = []
    monkeypatch.setattr(base_mod, "initial_stop_price", lambda d, p, cfg, atr: p * 0.95)
    monkeypatch.setattr(
        base_mod,
        "size_position",
        lambda **kw: SimpleNamespace(
            ok=True, quantity=0.4, base_units=40.0, notional_usd=28.0, risk_usd=0.5,
            effective_leverage=1.0,
        ),
    )  # fmt: skip
    fake = SimpleNamespace(
        id="S003",
        states={},
        stop_orders={},
        entry_skips={},
        rejects={},
        entries_attempted=0,
        entries_submitted=0,
        _venue_blocked=set(),
        _phantom_instruments={ADA},
        journal=None,
        instruments={ADA: SimpleNamespace()},
        atr={ADA: SimpleNamespace(initialized=True, value=0.02)},
        bars_seen={ADA: 500},
        exit_cfg=SimpleNamespace(tsl_enabled=False),
        limits=None,
        config=SimpleNamespace(trading_enabled=True, max_leverage=3.0, is_demo=False),
        cache=cache,
        log=SimpleNamespace(info=_quiet, warning=_quiet, error=_quiet, debug=_quiet),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"))),
        _jwrite=lambda what, fn: None,
        _guard_blocks_entry=lambda inst: False,
        _equity_usd=lambda: 100.0,
        _open_notional_usd=lambda: 0.0,
        _count_size_caps=lambda sized: None,
        _stop_attached_at_entry=lambda: False,
        _submit_entry=lambda inst, d, q, s: submitted.append((inst, d, q)) or "E-1",
    )
    fake._enter = NtxStrategy._enter.__get__(fake, NtxStrategy)
    return fake, submitted, events


def test_no_entry_on_an_instrument_whose_cache_holds_a_phantom(monkeypatch):
    fake, submitted, events = _entry_fake(monkeypatch, NettingCache(-0.4))
    fake._enter(ADA, Direction.LONG, SimpleNamespace(close=0.70))
    assert submitted == [], "an entry here nets against the phantom; the venue holds it alone"
    assert ADA not in fake.states
    assert fake.entry_skips.get("cache_not_flat") == 1, "never silent (R40)"
    assert ("entry_refused_cache_not_flat", "warning") in events


def test_no_entry_on_top_of_an_orphan_either(monkeypatch):
    """Not only a phantom: any cache position with no state is not ours to add to."""
    fake, submitted, _events = _entry_fake(monkeypatch, NettingCache(+0.4))
    fake._phantom_instruments = set()
    fake._enter(ADA, Direction.LONG, SimpleNamespace(close=0.70))
    assert submitted == []


def test_a_flat_cache_still_enters(monkeypatch):
    """The refusal must not cost the ordinary trade (gate parity, R67)."""
    fake, submitted, _events = _entry_fake(monkeypatch, NettingCache(0.0))
    fake._phantom_instruments = set()
    fake._enter(ADA, Direction.LONG, SimpleNamespace(close=0.70))
    assert submitted == [(ADA, Direction.LONG, 0.4)]


# --------------------------------------------------------------------------- #
# defence in depth: a cache closure is not a venue closure (R72)
# --------------------------------------------------------------------------- #
def _closed_fake(venue):
    closed: list = []
    cancelled: list = []
    events: list = []

    def positions():
        if venue is None:
            raise OSError("okx timeout")
        return venue

    fake = SimpleNamespace(
        states={},
        _pending_entry={},
        log=SimpleNamespace(info=_quiet, warning=_quiet, error=_quiet, debug=_quiet),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"))),
        _close_journal_trade=lambda inst, st, why, **kw: closed.append(why),
        _cancel_protective_stop=lambda inst: cancelled.append(inst),
        _close_confirmed=lambda inst, st: closed.append("confirmed"),
        _enter=lambda *a: None,
        venue_position_hook=positions,
    )
    for name in ("on_position_closed", "_venue_still_holds"):
        attr = getattr(NtxStrategy, name, None)
        if attr is not None:
            setattr(fake, name, attr.__get__(fake, NtxStrategy))
    fake.states[ADA] = PositionState(
        direction=Direction.LONG, entry_price=0.70, stop_price=0.665, margin_usd=9.0,
        meta={"filled_qty": 0.4, "trade_id": "t-9"},
    )  # fmt: skip
    return fake, closed, cancelled, events


@pytest.mark.parametrize("entry", [0.4, 0.6])
def test_the_netting_hazard_is_real(entry):
    """Model first: an entry of >= the phantom's size closes it in the cache."""
    cache = NettingCache(-0.4)
    assert cache.fill(+entry) is True, "nautilus emits PositionClosed for the phantom"


def test_a_phantom_netted_to_flat_does_not_cancel_the_real_stop():
    """The venue holds our +0.4; the cache's PositionClosed was the phantom's."""
    fake, closed, cancelled, events = _closed_fake({"ADA-USDT-SWAP": 0.4})
    fake.on_position_closed(SimpleNamespace(instrument_id=ADA))
    assert ADA in fake.states, "state dropped: nothing can exit this real position now"
    assert cancelled == [], "the venue stop on a REAL position was cancelled"
    assert closed == [], "journal says closed while the venue holds the position (R72)"
    assert ("cache_closed_venue_holds", "critical") in events


def test_a_real_external_close_is_still_finalised():
    """Hand-flatten / liquidation: the venue is flat, so the row closes (F047)."""
    fake, closed, cancelled, _events = _closed_fake({})
    fake.on_position_closed(SimpleNamespace(instrument_id=ADA))
    assert ADA not in fake.states
    assert closed == ["closed_externally"]
    assert cancelled == [ADA]


def test_an_unreadable_venue_keeps_the_stop():
    """Not knowing is not flat (R70); a stop on nothing is harmless, none on something is not."""
    fake, closed, cancelled, _events = _closed_fake(None)
    fake.on_position_closed(SimpleNamespace(instrument_id=ADA))
    assert ADA in fake.states
    assert cancelled == []
    assert closed == []
