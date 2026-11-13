"""Release a pre-restart OKX stop the node cannot see (F044).

nautilus's startup reconciliation does not load OKX algo (conditional) orders,
and OKX allows only ONE close-position stop per position. So on adoption the
old stop is cancelled here and its trigger price returned; the strategy then
submits a fresh, tracked stop at the same price (~1 s unprotected).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ntx.config.settings import Settings
from ntx.config.venues import OKX
from ntx.doctor import _signed_get
from ntx.ops.flatten import _signed_post


def _ours(o: dict) -> bool:
    """Our protective stop, in either form we place it:

    * fill-then-attach: a conditional, close-position (`closeFraction=1`) stop;
    * attached at entry: the OCO child of a bracket -- reduce-only, with a size.
    """
    if not o.get("slTriggerPx") or str(o.get("reduceOnly")).lower() != "true":
        return False
    return o.get("closeFraction") == "1" or bool(o.get("sz"))


def _pending(inst: str, creds, demo: bool, ord_type: str) -> list[dict]:
    r = _signed_get(
        OKX.auth_base_url,
        f"/api/v5/trade/orders-algo-pending?ordType={ord_type}&instId={inst}",
        creds,
        demo,
    )
    if r.get("code") != "0":
        raise RuntimeError(f"algo lookup failed: {r.get('code')} {r.get('msg')}")
    return r.get("data", [])


def _our_stop(inst: str, creds, demo: bool) -> dict | None:
    """The one close-position stop we own on this instrument, or None.

    Raises on anything ambiguous -- another trigger order, or more than one
    candidate -- so the caller never guesses which order to cancel (H-01).
    """
    pending = _pending(inst, creds, demo, "conditional") + _pending(inst, creds, demo, "oco")
    others = [o for o in pending if not _ours(o)]
    if others:
        raise RuntimeError(
            f"{inst}: {len(others)} trigger order(s) that are not our close-position stop "
            f"(algoIds {[o.get('algoId') for o in others]}) -- refusing to cancel blindly"
        )
    mine = [o for o in pending if _ours(o)]
    if not mine:
        return None
    if len(mine) > 1:
        raise RuntimeError(f"{inst}: {len(mine)} close-position stops found, expected one")
    return mine[0]


def peek_stop(instrument_id: str, settings: Settings, demo: bool) -> float | None:
    """The trigger price of our pre-restart stop, WITHOUT cancelling anything.

    Adoption plans with this and only cancels at the moment it re-places, so a
    position is never left naked while another instrument is still being planned
    (L-10). Raises on the same ambiguities as `release_stop`.
    """
    stop = _our_stop(instrument_id.removesuffix(".OKX"), settings.creds(), demo)
    return float(stop["slTriggerPx"]) if stop else None


def release_stop(
    instrument_id: str,
    settings: Settings,
    demo: bool,
    *,
    on_ambiguity: Callable[[str], None] | None = None,
) -> float | None:
    """Cancel our pre-restart stop and return its trigger price, or None.

    Raises on anything ambiguous *before* the cancel -- another trigger order on
    the instrument, more than one candidate, or a refused cancel. Nothing has
    changed at the venue at that point, so the caller can flatten rather than
    guess (H-01).

    **After an accepted cancel it never raises** (L2-05). The caller re-places the
    stop on the next line, and between those two lines the position has no
    protection: a timeout, a rate limit or one call leaving over IPv6 (F061) in
    the *verification* would otherwise abort adoption with the old stop already
    gone -- `_adopt` refuses to place a second one, the node exits, and systemd
    leaves a leveraged position naked for minutes.

    A cancel that did not really take effect is not lost either: OKX allows one
    close-position stop per position (F036), so the replacement is rejected --
    counted, alerted and repairable, unlike an exited node. `on_ambiguity` is
    called with a human-readable reason in that case.
    """
    inst = instrument_id.removesuffix(".OKX")
    creds = settings.creds()
    stop = _our_stop(inst, creds, demo)
    if stop is None:
        return None
    price = float(stop["slTriggerPx"])
    body = [{"algoId": stop["algoId"], "instId": inst}]
    c = _signed_post(OKX.auth_base_url, "/api/v5/trade/cancel-algos", body, creds, demo)
    if c.get("code") not in ("0", "2"):  # 2 = partial success, per-order codes decide
        raise RuntimeError(f"cancel failed: {c.get('code')} {c.get('msg')}")
    for row in c.get("data", []):
        # A batch can be accepted while an individual cancellation fails.
        if str(row.get("sCode", "0")) not in ("0", ""):
            raise RuntimeError(
                f"cancel of {row.get('algoId')} failed: sCode={row.get('sCode')} {row.get('sMsg')}"
            )

    # ---- the stop is gone from here on: report, never raise (L2-05) ---------- #
    note = ""
    try:
        still = [
            o
            for o in _pending(inst, creds, demo, "conditional") + _pending(inst, creds, demo, "oco")
            if _ours(o)
        ]
        if still:
            note = (
                f"{inst}: the venue still lists a stop "
                f"({[o.get('algoId') for o in still]}) after accepting the cancel of "
                f"{stop['algoId']}; placing the replacement anyway -- if the venue meant it, "
                f"the replacement is rejected and the watchdog repairs it"
            )
    except Exception as e:
        note = (
            f"{inst}: could not verify the cancel of {stop['algoId']} ({e}); the cancel was "
            f"accepted, so the replacement goes out regardless"
        )
    if note and on_ambiguity is not None:
        on_ambiguity(note)
    return price


def orphans(pending: list[dict], held: Iterable[str]) -> list[dict]:
    """Our close-position stops on instruments that hold no position (F065).

    A stop guarding nothing is not harmless. OKX allows exactly one close-position
    stop per position (F036), so the orphan refuses the NEXT position's stop on
    that instrument -- and nautilus never loads algo orders, so neither the order
    cache nor `stale_stops()` can see it. Only the venue knows it is there, and
    it only says so by rejecting.

    `held` carries our instrument ids (`SOL-USDT-SWAP.OKX`); the venue's `instId`
    has no venue suffix, so both forms are accepted.
    """
    keep = {str(i).removesuffix(".OKX") for i in held}
    return [o for o in pending if _ours(o) and o.get("instId") not in keep]


def _pending_account(creds, demo: bool, ord_type: str) -> list[dict]:
    """Every pending algo order of this type, across all instruments.

    One call per order type instead of one per instrument: the sweep runs at
    start-up over the whole universe, and 2 calls beat 20 against OKX's limits.
    """
    r = _signed_get(
        OKX.auth_base_url, f"/api/v5/trade/orders-algo-pending?ordType={ord_type}", creds, demo
    )
    if r.get("code") != "0":
        raise RuntimeError(f"algo lookup failed: {r.get('code')} {r.get('msg')}")
    return r.get("data", [])


def _venue_held(creds, demo: bool) -> set[str]:
    """Instruments the VENUE says hold a position (R27).

    The caller's `held` comes from nautilus's cache. If reconciliation failed
    quietly that set is empty, and reaping against it alone would cancel the stop
    on a real, open, leveraged position -- a far worse bug than the orphan this
    sweep exists to remove. So the venue is asked too, and only an instrument
    flat in BOTH views is swept.
    """
    r = _signed_get(OKX.auth_base_url, "/api/v5/account/positions?instType=SWAP", creds, demo)
    if r.get("code") != "0":
        raise RuntimeError(f"position lookup failed: {r.get('code')} {r.get('msg')}")
    return {p["instId"] for p in r.get("data", []) if float(p.get("pos") or 0)}


def venue_positions(settings: Settings, demo: bool) -> dict[str, float]:
    """What the VENUE says is held, as {instId: signed quantity}.

    Used to check a "closed while offline" before it is believed: nautilus's
    cache is filled asynchronously and an empty one is not evidence of a flat
    account (F066).
    """
    r = _signed_get(
        OKX.auth_base_url, "/api/v5/account/positions?instType=SWAP", settings.creds(), demo
    )
    if r.get("code") != "0":
        raise RuntimeError(f"position lookup failed: {r.get('code')} {r.get('msg')}")
    out: dict[str, float] = {}
    for p in r.get("data", []):
        qty = float(p.get("pos") or 0)
        if qty:
            out[str(p["instId"])] = qty
    return out


def reap_orphan_stops(held: Iterable[str], settings: Settings, demo: bool) -> list[str]:
    """Cancel every close-position stop of ours that guards no position.

    Returns the algoIds cancelled. Raises only if the venue could not be read or
    the cancel was refused outright -- the caller treats that as "try again next
    restart", never as a reason to refuse to start: an orphan costs a rejected
    stop later, a dead node costs everything now.
    """
    creds = settings.creds()
    safe = {str(i).removesuffix(".OKX") for i in held} | _venue_held(creds, demo)
    pending = _pending_account(creds, demo, "conditional") + _pending_account(creds, demo, "oco")
    dead = orphans(pending, safe)
    if not dead:
        return []
    body = [{"algoId": o["algoId"], "instId": o["instId"]} for o in dead]
    c = _signed_post(OKX.auth_base_url, "/api/v5/trade/cancel-algos", body, creds, demo)
    if c.get("code") not in ("0", "2"):
        raise RuntimeError(f"cancel failed: {c.get('code')} {c.get('msg')}")
    done = []
    for row in c.get("data", []):
        if str(row.get("sCode", "0")) in ("0", ""):
            done.append(str(row.get("algoId")))
    return done
