"""Position-sizing tests.

This is the most load-bearing arithmetic in the system: it decides how much money
is at stake on every trade. Each test names the rule or ADR it protects.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ntx.data.instruments import build_instrument
from ntx.risk.sizing import Reject, SizingLimits, min_notional_usd, size_position

SPECS = {
    "BTC": dict(
        instId="BTC-USDT-SWAP",
        ctVal="0.01",
        ctValCcy="BTC",
        minSz="0.01",
        lotSz="0.01",
        tickSz="0.1",
        lever="100",
    ),
    "SOL": dict(
        instId="SOL-USDT-SWAP",
        ctVal="1",
        ctValCcy="SOL",
        minSz="0.01",
        lotSz="0.01",
        tickSz="0.01",
        lever="100",
    ),
    "DOGE": dict(
        instId="DOGE-USDT-SWAP",
        ctVal="1000",
        ctValCcy="DOGE",
        minSz="0.01",
        lotSz="0.01",
        tickSz="0.00001",
        lever="50",
    ),
}
PRICE = {"BTC": 76_300.0, "SOL": 100.0, "DOGE": 0.082}


@pytest.fixture(params=list(SPECS))
def asset(request):
    name = request.param
    return name, build_instrument(SPECS[name]), PRICE[name]


def test_risk_is_honoured_across_contract_values(asset):
    """ADR-0003: risk per trade is ~risk_equity_pct of equity regardless of the
    instrument's contract value.

    This is the guard against the ct_val bug: DOGE's contract is 1000 DOGE, so
    dividing risk by stop distance without dividing by ct_val would oversize by
    1000x.
    """
    name, inst, price = asset
    equity = 100_000.0  # large enough that lot granularity is negligible
    target = equity * 0.01
    r = size_position(
        instrument=inst,
        price=price,
        stop_distance=price * 0.04,
        equity_usd=equity,
        limits=SizingLimits(risk_equity_pct=1.0, per_asset_pct_of_budget=100.0),
    )
    assert r.ok, f"{name}: {r.reject} {r.detail}"
    assert r.risk_usd == pytest.approx(target, rel=0.01), name


def test_rounding_never_over_risks(asset):
    """Lot granularity must only ever reduce risk, never increase it.

    At small equity a coarse lot size (BTC's 0.01 contracts = 0.0001 BTC) makes
    the realised risk noticeably below target -- that is correct and safe. The
    invariant is one-directional.
    """
    name, inst, price = asset
    for equity in (40.0, 100.0, 1_000.0, 10_000.0):
        r = size_position(
            instrument=inst, price=price, stop_distance=price * 0.04, equity_usd=equity
        )
        if not r.ok:
            continue
        target = equity * 0.01
        assert r.risk_usd <= target * 1.0001, (
            f"{name} at ${equity}: risk ${r.risk_usd:.4f} exceeds target ${target:.4f}"
        )


def test_base_units_equal_contracts_times_ct_val(asset):
    name, inst, price = asset
    r = size_position(instrument=inst, price=price, stop_distance=price * 0.04, equity_usd=1_000.0)
    assert r.base_units == r.quantity * Decimal(str(inst.multiplier)), name


def test_notional_matches_base_units_times_price(asset):
    name, inst, price = asset
    r = size_position(instrument=inst, price=price, stop_distance=price * 0.04, equity_usd=1_000.0)
    assert r.notional_usd == pytest.approx(float(r.base_units) * price, rel=1e-9), name


def test_tighter_stop_gives_larger_position():
    """The whole point of risk-based sizing: size adapts to stop distance so
    risk stays constant. Leverage-based sizing would return the same size."""
    inst = build_instrument(SPECS["SOL"])
    wide = size_position(instrument=inst, price=100.0, stop_distance=8.0, equity_usd=10_000.0)
    tight = size_position(instrument=inst, price=100.0, stop_distance=2.0, equity_usd=10_000.0)
    assert tight.quantity > wide.quantity
    assert tight.risk_usd == pytest.approx(wide.risk_usd, rel=0.02)
    assert tight.quantity == pytest.approx(wide.quantity * 4, rel=0.02)


def test_leverage_is_a_ceiling_not_a_target():
    """ADR-0003: with 1% risk and a 4% stop, effective leverage is ~0.25x --
    nowhere near the 3x ceiling. The ceiling must not inflate the position."""
    inst = build_instrument(SPECS["SOL"])
    r = size_position(
        instrument=inst,
        price=100.0,
        stop_distance=4.0,
        equity_usd=10_000.0,
        limits=SizingLimits(max_leverage=3.0),
    )
    assert r.ok
    assert r.effective_leverage < 0.5
    r10 = size_position(
        instrument=inst,
        price=100.0,
        stop_distance=4.0,
        equity_usd=10_000.0,
        limits=SizingLimits(max_leverage=10.0),
    )
    assert r10.quantity == r.quantity, "raising the ceiling must not change the size"


def test_leverage_ceiling_shrinks_an_oversized_trade():
    inst = build_instrument(SPECS["SOL"])
    # A very tight stop would otherwise demand a huge position.
    r = size_position(
        instrument=inst,
        price=100.0,
        stop_distance=0.01,
        equity_usd=1_000.0,
        limits=SizingLimits(max_leverage=2.0, per_asset_pct_of_budget=100.0),
    )
    assert r.ok
    assert r.effective_leverage == pytest.approx(2.0, rel=0.02)
    assert "leverage ceiling" in r.detail


def test_per_asset_cap_applies():
    inst = build_instrument(SPECS["SOL"])
    r = size_position(
        instrument=inst,
        price=100.0,
        stop_distance=0.01,
        equity_usd=1_000.0,
        limits=SizingLimits(max_leverage=3.0, per_asset_pct_of_budget=40.0),
    )
    assert r.ok
    assert r.notional_usd == pytest.approx(1_000.0 * 3.0 * 0.40, rel=0.02)
    assert "per-asset cap" in r.detail


def test_existing_exposure_reduces_remaining_budget():
    inst = build_instrument(SPECS["SOL"])
    r = size_position(
        instrument=inst,
        price=100.0,
        stop_distance=0.01,
        equity_usd=1_000.0,
        open_notional_usd=2_900.0,
        limits=SizingLimits(max_leverage=3.0, per_asset_pct_of_budget=100.0),
    )
    assert r.ok
    assert r.notional_usd == pytest.approx(100.0, rel=0.05)


def test_rejects_when_ceiling_already_reached():
    inst = build_instrument(SPECS["SOL"])
    r = size_position(
        instrument=inst,
        price=100.0,
        stop_distance=4.0,
        equity_usd=1_000.0,
        open_notional_usd=3_000.0,
        limits=SizingLimits(max_leverage=3.0),
    )
    assert r.reject is Reject.LEVERAGE_CEILING
    assert not r.ok


def test_max_concurrent_positions_gate():
    inst = build_instrument(SPECS["SOL"])
    r = size_position(
        instrument=inst,
        price=100.0,
        stop_distance=4.0,
        equity_usd=1_000.0,
        open_positions=3,
        limits=SizingLimits(max_concurrent_positions=3),
    )
    assert r.reject is Reject.MAX_POSITIONS


def test_free_margin_buffer_applies():
    inst = build_instrument(SPECS["SOL"])
    r = size_position(
        instrument=inst,
        price=100.0,
        stop_distance=0.01,
        equity_usd=1_000.0,
        free_margin_usd=100.0,
        limits=SizingLimits(max_leverage=3.0, per_asset_pct_of_budget=100.0),
    )
    assert r.ok
    # margin per contract = 100/3; usable = 90 -> notional <= 90*3 = 270
    assert r.notional_usd <= 270.0 * 1.01
    assert "free margin" in r.detail


def test_tiny_equity_rejects_rather_than_widening():
    """R6: never widen a position to reach a venue minimum. Reject instead."""
    inst = build_instrument(SPECS["BTC"])
    r = size_position(
        instrument=inst, price=76_300.0, stop_distance=76_300.0 * 0.04, equity_usd=0.50
    )
    assert not r.ok
    assert r.reject in (Reject.BELOW_MIN_QTY, Reject.BELOW_MIN_NOTIONAL)
    assert r.quantity == 0


def test_real_account_tradability_at_the_chosen_risk(asset):
    """At the user's chosen 0.5% risk, $40.0 cannot size BTC.

    Risk-based sizing means a smaller risk budget buys a smaller position, so
    lowering risk *reduces* the tradable universe at a fixed equity. Measured on
    live OKX specs: 8 of 10 assets work at 0.5%, all 10 at 1.0%. BTC needs ~$60
    of equity at 0.5% and BNB ~$57.

    This is the trade-off of the safer risk setting, not a defect -- and
    `preflight.py` drops the infeasible instruments rather than letting the node
    silently reject every order.
    """
    name, inst, price = asset
    stop = price * 0.04

    at_half = size_position(
        instrument=inst,
        price=price,
        stop_distance=stop,
        equity_usd=40.0,
        limits=SizingLimits(risk_equity_pct=0.5),
    )
    at_one = size_position(
        instrument=inst,
        price=price,
        stop_distance=stop,
        equity_usd=40.0,
        limits=SizingLimits(risk_equity_pct=1.0),
    )

    # 1.0% risk sizes every asset in the fixture set.
    assert at_one.ok, f"{name} unsizeable at 1.0% risk: {at_one.reject} {at_one.detail}"

    if name == "BTC":
        assert not at_half.ok, "BTC should NOT be sizeable at 0.5% risk on $40.0"
        assert at_half.reject in (Reject.BELOW_MIN_QTY, Reject.BELOW_MIN_NOTIONAL)
    else:
        assert at_half.ok, f"{name} should be sizeable at 0.5%: {at_half.detail}"
        assert at_half.notional_usd >= min_notional_usd(inst, price)


def test_lower_risk_shrinks_the_tradable_universe():
    """Counter-intuitive but important: halving risk halves position size, so a
    *safer* setting makes fewer instruments reachable at small equity."""
    inst = build_instrument(SPECS["BTC"])
    stop = 76_300.0 * 0.04
    lo = size_position(
        instrument=inst,
        price=76_300.0,
        stop_distance=stop,
        equity_usd=40.0,
        limits=SizingLimits(risk_equity_pct=0.5),
    )
    hi = size_position(
        instrument=inst,
        price=76_300.0,
        stop_distance=stop,
        equity_usd=40.0,
        limits=SizingLimits(risk_equity_pct=1.0),
    )
    assert not lo.ok and hi.ok
    # and the rejection must say how much equity would be needed
    assert "minimum" in lo.detail or "equity" in lo.detail


def test_quantity_respects_lot_size(asset):
    name, inst, price = asset
    r = size_position(instrument=inst, price=price, stop_distance=price * 0.037, equity_usd=537.0)
    assert r.ok, name
    step = Decimal(str(inst.size_increment))
    assert r.quantity % step == 0, f"{name}: {r.quantity} is not a multiple of {step}"


def test_rounding_is_down_so_caps_are_never_exceeded():
    inst = build_instrument(SPECS["SOL"])
    limits = SizingLimits(max_leverage=3.0, per_asset_pct_of_budget=40.0)
    r = size_position(
        instrument=inst, price=100.0, stop_distance=0.01, equity_usd=1_000.0, limits=limits
    )
    cap = 1_000.0 * 3.0 * 0.40
    assert r.notional_usd <= cap, "rounding must never push the size above a cap"


@pytest.mark.parametrize(
    ("price", "stop", "equity"),
    [
        (0.0, 4.0, 1000.0),
        (-1.0, 4.0, 1000.0),
        (100.0, 0.0, 1000.0),
        (100.0, -4.0, 1000.0),
        (100.0, 4.0, 0.0),
        (100.0, 4.0, -5.0),
    ],
)
def test_bad_input_rejected_not_divided_by_zero(price, stop, equity):
    """A flat synthetic series gives ATR == 0; that must not divide by zero and
    produce an infinite position."""
    inst = build_instrument(SPECS["SOL"])
    r = size_position(instrument=inst, price=price, stop_distance=stop, equity_usd=equity)
    assert r.reject is Reject.BAD_INPUT
    assert r.quantity == 0


# ------------- SizingResult.ok (mutation survivor: `and` -> `or`) ----------- #
def test_ok_requires_both_no_reject_AND_positive_quantity():
    """Mutation testing found `and` -> `or` survived here.

    The conjunction guards a silent bad state: a result with no recorded
    rejection but a zero quantity would otherwise report itself as usable, and
    the caller would submit an order for nothing.
    """
    from decimal import Decimal as D

    from ntx.risk.sizing import Reject, SizingResult

    good = SizingResult(D("1"), D("1"), 100.0, 1.0, 0.1, Reject.NONE, "")
    assert good.ok

    no_reject_but_zero_qty = SizingResult(D(0), D(0), 0.0, 0.0, 0.0, Reject.NONE, "")
    assert not no_reject_but_zero_qty.ok, "zero quantity is never usable"

    rejected_but_positive_qty = SizingResult(
        D("1"), D("1"), 100.0, 1.0, 0.1, Reject.LEVERAGE_CEILING, ""
    )
    assert not rejected_but_positive_qty.ok, "a rejection is never usable"

    both_bad = SizingResult(D(0), D(0), 0.0, 0.0, 0.0, Reject.BAD_INPUT, "")
    assert not both_bad.ok
