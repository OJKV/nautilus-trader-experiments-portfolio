"""F069: "closing" a position that does not exist OPENS one.

Twice on the live account, 2026-09-28 10:20 and 2026-09-29 04:00:

    ADOPTION flatten LINK-USDT-SWAP.OKX: no journal row and no venue stop
    SubmitOrder(MarketOrder(BUY 0.6 LINK-USDT-SWAP.OKX MARKET GTC))
    OrderFilled

The venue held no LINK at either moment -- verified three minutes before the
first one. nautilus's cache thought it held a SHORT, so "flatten" meant BUY, and
because there was nothing to buy back, the buy opened a real long position of
real money with no stop on it.

Where the phantom short comes from: nautilus reconciles fills over a limited
lookback window. It saw the *closing* sells -- from a trailing stop on 09-27, and
from the owner's own manual flatten on 09-29 -- without the opening buys, which
were outside the window. Applied to an empty cache, a lone sell is a short.

That makes it a loop, which is the worst part: every manual close teaches the
node it went short, and the node "repairs" that by buying, which re-opens the
position the owner just closed. The owner closed LINK; two minutes later the node
bought it straight back.

F066 checked the venue before writing "closed" into the journal. This is the same
disagreement pointing the other way -- cache says open, venue says flat -- and it
is far worse, because that path does not write a row, it **sends an order**.
Nothing may close a position on the strength of the cache alone.

Written before the fix (R41).
"""

from __future__ import annotations

from types import SimpleNamespace

from ntx.strategies.base import NtxStrategy

LINK = "LINK-USDT-SWAP.OKX"


def _position(signed_qty: float):
    return SimpleNamespace(signed_qty=signed_qty, quantity=abs(signed_qty))


def _fake(*, cached, venue, hook=True):
    closed: list = []
    events: list = []
    logged: list = []

    def positions():
        if venue is None:
            raise OSError("okx timeout")
        return venue

    fake = SimpleNamespace(
        instruments={LINK: None},
        states={},
        stop_orders={},
        stop_client_order_ids={},
        journal=None,
        log=SimpleNamespace(
            info=lambda *a: None,
            warning=lambda *a: logged.append(str(a[0]) if a else ""),
            error=lambda *a: logged.append(str(a[0]) if a else ""),
            debug=lambda *a: None,
        ),
        _jevent=lambda kind, **kw: events.append((kind, kw.get("severity"))),
        _venue_writes_allowed=lambda why: True,
        cache=SimpleNamespace(
            positions_open=lambda instrument_id=None: (
                [_position(cached)] if cached and str(instrument_id) == LINK else []
            )
        ),
        cancel_all_orders=lambda inst: None,
        close_position=lambda p: closed.append(p.signed_qty),
        venue_position_hook=positions if hook else None,
    )
    fake._venue_agrees_position = NtxStrategy._venue_agrees_position.__get__(fake, NtxStrategy)
    NtxStrategy._flatten.__get__(fake, NtxStrategy)(LINK)
    return closed, events, logged


def test_a_position_the_venue_really_holds_is_still_closed():
    closed, _events, _logged = _fake(cached=0.6, venue={"LINK-USDT-SWAP": 0.6})
    assert closed == [0.6], "a real position must still be closable"


def test_a_short_the_venue_really_holds_is_still_closed():
    closed, _events, _logged = _fake(cached=-0.6, venue={"LINK-USDT-SWAP": -0.6})
    assert closed == [-0.6]


def test_a_phantom_position_is_never_closed():
    """The exact 2026-09-29 04:00 sequence: cache short, venue flat."""
    closed, events, _logged = _fake(cached=-0.6, venue={})
    assert closed == [], "closing a position the venue does not have OPENS one"
    assert ("phantom_flatten_refused", "critical") in events


def test_a_position_the_venue_holds_the_other_way_is_never_closed():
    """Cache says short, venue says long: closing the cache's view doubles the real one."""
    closed, events, _logged = _fake(cached=-0.6, venue={"LINK-USDT-SWAP": 0.6})
    assert closed == []
    assert ("phantom_flatten_refused", "critical") in events


def test_a_venue_we_cannot_read_does_not_send_the_order():
    """R70. Not knowing is not "it is there" -- and this path spends money."""
    closed, events, _logged = _fake(cached=-0.6, venue=None)
    assert closed == []
    assert ("phantom_flatten_refused", "critical") in events


def test_the_refusal_names_the_instrument_and_both_views():
    _closed, _events, logged = _fake(cached=-0.6, venue={})
    line = " ".join(logged)
    assert "LINK" in line and "-0.6" in line


def test_without_a_hook_the_old_behaviour_is_unchanged():
    """A backtest has no venue hook; it must not be crippled by this guard."""
    closed, _events, _logged = _fake(cached=-0.6, venue={}, hook=False)
    assert closed == [-0.6]


# ---- size matters too, not only direction (found reviewing the fix live) --- #


def test_closing_more_than_the_venue_holds_is_refused():
    """A close bigger than the position opens the remainder the other way.

    The first version of this guard only compared the SIGN. That is enough while
    the venue is flat, but not once a phantom and a real position coexist on one
    instrument: cache 1.2 long against a real 0.6 long is "same side", and
    closing 1.2 sells 0.6 that exists and 0.6 that does not -- leaving a short.
    Exactly the F069 failure, one step further along.
    """
    closed, events, _logged = _fake(cached=1.2, venue={"LINK-USDT-SWAP": 0.6})
    assert closed == []
    assert ("phantom_flatten_refused", "critical") in events


def test_closing_less_than_the_venue_holds_is_allowed():
    """Closing part of a real position leaves a real remainder: safe, and the
    watchdog repairs the rest. Only overshoot creates a position."""
    closed, _events, _logged = _fake(cached=0.6, venue={"LINK-USDT-SWAP": 1.2})
    assert closed == [0.6]


def test_a_rounding_difference_is_not_an_overshoot():
    closed, _events, _logged = _fake(cached=0.6, venue={"LINK-USDT-SWAP": 0.6 - 1e-12})
    assert closed == [0.6]
