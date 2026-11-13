"""Startup and watchdog reconciliation.

The node may restart at any moment -- crash, deploy, reboot, systemd -- while
positions are open. The venue is **authoritative**; local state is a cache that
may be stale, wrong, or absent (R27).

This is what makes `Restart=always` safe. Without it, auto-restart would let the
node re-enter positions it already holds, and the 246-ghost-position failure in
the field report (P-004) is what that looks like after a few months.

Pure decision logic: it takes two snapshots and returns the actions to take, so
every branch is testable without a venue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Action(StrEnum):
    ADOPT = "adopt"  # venue has it, we did not -- take ownership
    ATTACH_STOP = "attach_stop"  # position exists with no protective order
    FLATTEN_UNCLAIMED = "flatten_unclaimed"  # nobody owns it -- close it
    DROP_STALE = "drop_stale"  # we think we hold it, venue disagrees
    OK = "ok"


@dataclass(frozen=True)
class VenuePosition:
    instrument_id: str
    quantity: float  # signed: positive long, negative short
    entry_price: float
    has_protective_stop: bool = False


@dataclass(frozen=True)
class LocalPosition:
    instrument_id: str
    quantity: float
    strategy_id: str


@dataclass(frozen=True)
class Decision:
    instrument_id: str
    action: Action
    reason: str
    venue_quantity: float | None = None
    local_quantity: float | None = None

    @property
    def is_fatal(self) -> bool:
        """A quantity mismatch means we cannot know our own exposure."""
        return self.action is Action.DROP_STALE and self.local_quantity not in (None, 0.0)


@dataclass
class Report:
    decisions: list[Decision] = field(default_factory=list)
    unprotected: int = 0
    adopted: int = 0
    flattened: int = 0
    stale: int = 0

    @property
    def ok(self) -> bool:
        return self.unprotected == 0 and self.stale == 0

    def summary(self) -> str:
        return (
            f"reconcile: {len(self.decisions)} instrument(s) reviewed, "
            f"adopted={self.adopted} unprotected={self.unprotected} "
            f"flattened={self.flattened} stale={self.stale} "
            f"-> {'OK' if self.ok else 'ATTENTION'}"
        )


def reconcile(
    venue: list[VenuePosition],
    local: list[LocalPosition],
    *,
    known_strategies: set[str] | None = None,
    qty_tolerance: float = 1e-9,
) -> Report:
    """Compare venue truth against local belief and decide what to do.

    Rules, in order of severity:

    1. Venue has a position we do not know about -> **adopt** it (it is real
       money at risk), then ensure it has a stop.
    2. A position without a venue-side stop -> **attach one**. R15 requires
       `positions_unprotected` to be zero.
    3. A position claimed by no known strategy -> **flatten**. An unowned
       leveraged position will never be managed.
    4. We believe we hold something the venue does not -> **drop** the local
       record. If our recorded quantity was non-zero this is fatal: we cannot
       trust our own view of exposure, so the node must refuse to trade rather
       than guess (fail closed).
    """
    report = Report()
    known = known_strategies or set()
    by_venue = {p.instrument_id: p for p in venue}
    by_local = {p.instrument_id: p for p in local}

    for inst, vp in by_venue.items():
        lp = by_local.get(inst)
        if lp is None:
            report.adopted += 1
            report.decisions.append(
                Decision(
                    inst,
                    Action.ADOPT,
                    "venue holds a position the node did not know about",
                    vp.quantity,
                    None,
                )
            )
        elif abs(vp.quantity - lp.quantity) > qty_tolerance:
            report.adopted += 1
            report.decisions.append(
                Decision(
                    inst,
                    Action.ADOPT,
                    f"quantity mismatch: venue {vp.quantity} vs local {lp.quantity}; "
                    f"venue is authoritative",
                    vp.quantity,
                    lp.quantity,
                )
            )
        elif lp.strategy_id not in known:
            report.flattened += 1
            report.decisions.append(
                Decision(
                    inst,
                    Action.FLATTEN_UNCLAIMED,
                    f"claimed by unknown strategy {lp.strategy_id!r}",
                    vp.quantity,
                    lp.quantity,
                )
            )
            continue
        else:
            report.decisions.append(Decision(inst, Action.OK, "in sync", vp.quantity, lp.quantity))

        if not vp.has_protective_stop:
            report.unprotected += 1
            report.decisions.append(
                Decision(
                    inst,
                    Action.ATTACH_STOP,
                    "position has no venue-side protective stop (R15)",
                    vp.quantity,
                    lp.quantity if lp else None,
                )
            )

    for inst, lp in by_local.items():
        if inst in by_venue:
            continue
        report.stale += 1
        report.decisions.append(
            Decision(
                inst,
                Action.DROP_STALE,
                "node believes it holds a position the venue does not report",
                None,
                lp.quantity,
            )
        )
    return report


# --------------------------------------------------------------------------- #
# Restart adoption (gap G1): turn venue truth + journal memory into strategy
# state. Pure, like `reconcile`; `NtxStrategy._adopt_positions` executes it.
# --------------------------------------------------------------------------- #
class AdoptAction(StrEnum):
    ADOPT = "adopt"  # manage this position; attach a stop if `needs_stop`
    FLATTEN = "flatten"  # cannot be protected sensibly -- close it
    CLOSE_JOURNAL = "close_journal"  # closed while the node was down


@dataclass(frozen=True)
class Adoption:
    instrument_id: str
    action: AdoptAction
    reason: str
    direction: object = None  # ntx.strategies.signal.Direction
    quantity: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    needs_stop: bool = False
    trade_id: str | None = None
    # True when `stop_price` came from a stop that is STILL LIVE at the venue and
    # invisible to the cache (OKX algo orders, F044). The caller must cancel it
    # immediately before placing the replacement, never earlier (L-10).
    releases_venue_stop: bool = False
    # Set when a venue stop exists but sits away from the stop THIS trade asked
    # for (journal) by more than STOP_DIVERGENCE_PCT: the caller decides whether
    # to replace it (R71; review 2026-10-11 F-03).
    journal_stop: float | None = None


# Same tolerance as the strategy's stale-stop check (base.STALE_STOP_TOLERANCE_PCT).
STOP_DIVERGENCE_PCT = 0.1


def adoption_plan(
    venue: list[VenuePosition],
    journal_open: list[dict],
    venue_stops: dict[str, float],
    released_stops: dict[str, float] | None = None,
    qty_tolerance: float = 1e-9,
) -> list[Adoption]:
    """Decide what a starting node does with each position it finds.

    The venue says WHAT is held; the journal says WHY (entry, stop). Rules:

    - held + journal, same side  -> adopt; stop = live venue stop if any, else
      the journal stop (attached now)
    - held, no journal, venue stop live -> adopt at the venue's average price
      with that stop (e.g. a crash before the journal write)
    - held, no journal, no stop  -> flatten: nobody knows where its stop belongs
    - held on the OTHER side than the journal -> the journal stop is on the
      wrong side; adopt only if a venue stop exists, else flatten
    - journal open, venue flat   -> close the journal row (a stop that fired
      while the node was down is normal, not fatal)

    `venue_stops` are stops the node already tracks (kept as they are).
    `released_stops` are pre-restart venue stops the node could not see and
    cancelled via REST (F044): their price is the truth, but a fresh stop must be
    submitted.
    """
    released = released_stops or {}
    from ntx.strategies.signal import Direction

    rows = {r["instrument_id"]: r for r in journal_open}
    out: list[Adoption] = []
    held = set()
    for vp in venue:
        if vp.quantity == 0:
            continue
        inst = vp.instrument_id
        held.add(inst)
        direction = Direction.LONG if vp.quantity > 0 else Direction.SHORT
        qty = abs(vp.quantity)
        venue_stop = venue_stops.get(inst)
        known_stop = venue_stop if venue_stop is not None else released.get(inst)
        row = rows.get(inst)
        same_side = row is not None and str(row["side"]).upper().endswith(direction.name)
        if row is not None and same_side:
            stop = known_stop if known_stop is not None else float(row["stop_price"])
            # Side is not size. Comparing only the side let a venue position of
            # 2.02 read as "agree" against a journal row of 1.06, so every risk
            # number derived from the row was half the truth (F066). The venue
            # wins on quantity (R27) -- we manage what is actually there -- but
            # it must be said out loud, not absorbed.
            row_qty = abs(float(row["qty"] or 0.0))
            agrees = abs(row_qty - qty) <= max(qty_tolerance, qty * 1e-9)
            reason = (
                "journal + venue agree"
                if agrees
                else f"venue holds {qty} against {row_qty} in the journal; the venue is the truth"
            )
            own = row["stop_price"]
            own = float(own) if own not in (None, "") else None
            diverges = (
                known_stop is not None and own
                and abs(known_stop - own) / abs(own) * 100.0 > STOP_DIVERGENCE_PCT
            )  # fmt: skip
            out.append(
                Adoption(inst, AdoptAction.ADOPT, reason, direction, qty,
                         float(row["entry_price"]), stop, venue_stop is None, row["trade_id"],
                         venue_stop is None and inst in released,
                         own if diverges else None)
            )  # fmt: skip
        elif known_stop is not None:
            why = "side differs from journal" if row is not None else "no journal row"
            out.append(
                Adoption(inst, AdoptAction.ADOPT, f"{why}; venue stop price kept", direction,
                         qty, vp.entry_price, known_stop, venue_stop is None,
                         row["trade_id"] if row is not None else None,
                         venue_stop is None and inst in released)
            )  # fmt: skip
        else:
            why = "side differs from journal" if row is not None else "no journal row"
            out.append(
                Adoption(inst, AdoptAction.FLATTEN, f"{why} and no venue stop", direction, qty)
            )
    for inst, row in rows.items():
        if inst not in held:
            out.append(
                Adoption(inst, AdoptAction.CLOSE_JOURNAL, "venue flat: closed while offline",
                         trade_id=row["trade_id"])
            )  # fmt: skip
    return out
