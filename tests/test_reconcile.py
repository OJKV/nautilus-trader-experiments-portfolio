"""Reconciliation tests.

This logic is what makes systemd `Restart=always` safe. Every branch is tested
because the failure mode -- a node re-entering positions it already holds, or
trading while blind to its own exposure -- is unrecoverable with real money.
"""

from __future__ import annotations

from ntx.live.reconcile import Action, LocalPosition, VenuePosition, reconcile

KNOWN = {"S003"}


def _v(inst="SOL-USDT-SWAP.OKX", qty=1.0, protected=True) -> VenuePosition:
    return VenuePosition(inst, qty, 100.0, protected)


def _l(inst="SOL-USDT-SWAP.OKX", qty=1.0, strat="S003") -> LocalPosition:
    return LocalPosition(inst, qty, strat)


def test_in_sync_and_protected_is_ok():
    r = reconcile([_v()], [_l()], known_strategies=KNOWN)
    assert r.ok
    assert [d.action for d in r.decisions] == [Action.OK]


def test_unknown_venue_position_is_adopted():
    """Restart with a position open: the venue has it, we do not. It is real
    money, so take ownership rather than ignore it."""
    r = reconcile([_v()], [], known_strategies=KNOWN)
    assert r.adopted == 1
    assert any(d.action is Action.ADOPT for d in r.decisions)


def test_unprotected_position_is_flagged_and_stop_attached():
    """R15: positions_unprotected must be zero."""
    r = reconcile([_v(protected=False)], [_l()], known_strategies=KNOWN)
    assert r.unprotected == 1
    assert not r.ok
    assert any(d.action is Action.ATTACH_STOP for d in r.decisions)


def test_adopted_position_without_stop_gets_both_actions():
    r = reconcile([_v(protected=False)], [], known_strategies=KNOWN)
    actions = [d.action for d in r.decisions]
    assert Action.ADOPT in actions and Action.ATTACH_STOP in actions
    assert r.adopted == 1 and r.unprotected == 1


def test_quantity_mismatch_adopts_venue_truth():
    r = reconcile([_v(qty=2.0)], [_l(qty=1.0)], known_strategies=KNOWN)
    d = next(x for x in r.decisions if x.action is Action.ADOPT)
    assert d.venue_quantity == 2.0 and d.local_quantity == 1.0
    assert "authoritative" in d.reason


def test_unclaimed_position_is_flattened():
    """An unowned leveraged position will never be managed by anything."""
    r = reconcile([_v()], [_l(strat="S999")], known_strategies=KNOWN)
    assert r.flattened == 1
    assert any(d.action is Action.FLATTEN_UNCLAIMED for d in r.decisions)


def test_unclaimed_position_skips_stop_attachment():
    """It is about to be closed, so do not also protect it."""
    r = reconcile([_v(protected=False)], [_l(strat="S999")], known_strategies=KNOWN)
    assert Action.ATTACH_STOP not in [d.action for d in r.decisions]


def test_stale_local_record_is_dropped_and_is_fatal():
    """We think we hold something the venue does not report. That means we cannot
    trust our own exposure, so it must fail closed rather than guess."""
    r = reconcile([], [_l(qty=1.0)], known_strategies=KNOWN)
    assert r.stale == 1
    assert not r.ok
    d = next(x for x in r.decisions if x.action is Action.DROP_STALE)
    assert d.is_fatal


def test_zero_quantity_stale_record_is_not_fatal():
    r = reconcile([], [_l(qty=0.0)], known_strategies=KNOWN)
    d = next(x for x in r.decisions if x.action is Action.DROP_STALE)
    assert not d.is_fatal


def test_flat_both_sides_is_ok():
    r = reconcile([], [], known_strategies=KNOWN)
    assert r.ok and r.decisions == []


def test_multiple_instruments_are_each_decided():
    venue = [_v("A.OKX", 1.0, True), _v("B.OKX", -2.0, False)]
    local = [_l("A.OKX", 1.0), _l("C.OKX", 3.0)]
    r = reconcile(venue, local, known_strategies=KNOWN)
    assert r.adopted == 1  # B unknown locally
    assert r.unprotected == 1  # B has no stop
    assert r.stale == 1  # C is local-only
    assert not r.ok


def test_short_positions_handled_by_sign():
    r = reconcile([_v(qty=-1.0)], [_l(qty=-1.0)], known_strategies=KNOWN)
    assert r.ok


def test_summary_is_human_readable():
    r = reconcile([_v(protected=False)], [], known_strategies=KNOWN)
    s = r.summary()
    assert "unprotected=1" in s and "ATTENTION" in s


def test_quantity_mismatch_adoption_is_counted():
    """Review finding: the mismatch branch emitted ADOPT without counting it, so
    the summary said adopted=0 while an adoption happened."""
    from ntx.live.reconcile import LocalPosition, VenuePosition, reconcile

    r = reconcile(
        [VenuePosition("SOL", 2.0, 100.0, has_protective_stop=True)],
        [LocalPosition("SOL", 1.0, "S003")],
        known_strategies={"S003"},
    )
    assert r.adopted == 1
    assert "adopted=1" in r.summary()
