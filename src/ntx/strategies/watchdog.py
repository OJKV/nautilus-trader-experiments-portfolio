"""Protection watchdog decision logic (R15).

Pure, so it is tested without a venue. `NtxStrategy` calls it on a timer.

Why a watchdog at all: OKX's execution client cannot attach a stop to the entry
order (F007), so protection is fill-then-attach -- and a stop can also be
cancelled, expire or be rejected long after it was placed, without the strategy
being told in a way it acts on. The dangerous case is a stop that our records
still list while it is closed at the venue: our bookkeeping says "protected"
and the position is naked.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping


def needs_protection(
    open_positions: Iterable[str],
    stop_ids: Mapping[str, object],
    is_stop_live: Callable[[object], bool],
) -> list[str]:
    """Instruments holding a position without a confirmed-live stop.

    Every position is checked (R23). If a stop's status cannot be confirmed the
    position is reported unprotected -- a guard must fail closed.
    """
    out: list[str] = []
    for inst in open_positions:
        stop = stop_ids.get(inst)
        if stop is None:
            out.append(inst)
            continue
        try:
            live = bool(is_stop_live(stop))
        except Exception:
            live = False
        if not live:
            out.append(inst)
    return out


def stop_topup_needed(filled_qty: float, covered_qty: float) -> float | None:
    """Stop quantity to (re)submit after an entry fill, or None if covered.

    Entry orders can fill in pieces. The stop must cover the WHOLE filled
    position, so after every fill the answer is either "nothing" or "a stop for
    everything filled so far" -- never a stop for just the last piece (F030).
    Over-coverage is harmless: protective stops are reduce-only.
    """
    if filled_qty <= 0 or covered_qty >= filled_qty:
        return None
    return filled_qty


def stale_stops(open_stop_ids: Iterable[object], tracked: Iterable[object]) -> list:
    """Open stop orders this strategy no longer tracks (review L2).

    A cancel that failed leaves such an order live at the venue. On OKX a
    close-position stop also blocks any NEW stop on that instrument (one per
    position), so an orphan silently prevents protection. The watchdog cancels
    every one it finds.
    """
    known = set(tracked)
    return [oid for oid in open_stop_ids if oid not in known]


def data_stalled(last_bar_ns: int | None, now_ns: int, *, interval_ns: int, grace_ns: int) -> bool:
    """True if live bars have stopped: none for two intervals plus a grace.

    None (no live bar yet) is not a stall -- warm-up and the first interval
    legitimately produce nothing.
    """
    if last_bar_ns is None:
        return False
    return now_ns - last_bar_ns > 2 * interval_ns + grace_ns
