"""Position sizing: risk-based, leverage-capped.

The rule (ADR-0003, R5): size from **risk**, never from leverage.

    risk_usd      = equity * risk_equity_pct
    stop_distance = sl_atr_mult * ATR            (price units)
    base_units    = risk_usd / stop_distance
    contracts     = base_units / ct_val          (OKX sizes in contracts)

`max_leverage` is a **ceiling** that only ever rejects or shrinks a trade. Sizing
from leverage instead gives a tight stop and a wide stop the same position size,
so risk per trade swings uncontrollably with volatility -- the standard way small
futures accounts die.

The contract-multiplier step is the subtle part and a genuine bug source: on OKX
one contract is `ct_val` base units, so DOGE's ct_val of 1000 means a single
contract is 1000 DOGE. Dividing risk by stop distance alone would oversize by
1000x. `ct_val` is carried in nautilus's `multiplier`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from nautilus_trader.model.instruments import Instrument


class Reject(StrEnum):
    """Why a trade was not sized. Each maps to a gate in docs/08 §5."""

    NONE = "none"
    BELOW_MIN_QTY = "below_min_qty"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    LEVERAGE_CEILING = "leverage_ceiling"
    PER_ASSET_CAP = "per_asset_cap"
    MAX_POSITIONS = "max_positions"
    FREE_MARGIN = "free_margin"
    BAD_INPUT = "bad_input"


@dataclass(frozen=True)
class SizingLimits:
    # 0.5% chosen by the user 2026-09-16 (R14). Sharpe is flat across risk
    # levels; this only sets how deep the losing stretches go. At $37 that is
    # $0.19 per trade, a ~16% historical drawdown and ~23% in an unlucky
    # sequence -- inside the 40% gate limit, unlike 1.0% which breached it.
    risk_equity_pct: float = 0.5
    max_leverage: float = 3.0
    max_concurrent_positions: int = 3
    per_asset_pct_of_budget: float = 40.0
    free_margin_buffer: float = 0.9  # use at most 90% of free margin
    min_notional_headroom: float = 1.0  # multiple of the venue minimum


# Module-level singleton: SizingLimits is frozen, but a call in a default
# argument is evaluated once at import and reads as a mutable default (ruff B008).
DEFAULT_LIMITS = SizingLimits()


@dataclass(frozen=True)
class SizingResult:
    quantity: Decimal  # in CONTRACTS, already rounded to lot size
    base_units: Decimal  # quantity * ct_val
    notional_usd: float
    risk_usd: float
    effective_leverage: float
    reject: Reject = Reject.NONE
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.reject is Reject.NONE and self.quantity > 0


def _zero(reject: Reject, detail: str = "") -> SizingResult:
    return SizingResult(Decimal(0), Decimal(0), 0.0, 0.0, 0.0, reject, detail)


def min_notional_usd(inst: Instrument, price: float) -> float:
    """Smallest order value the venue accepts: min_qty * ct_val * price."""
    return float(inst.min_quantity) * float(inst.multiplier) * price


def size_position(
    *,
    instrument: Instrument,
    price: float,
    stop_distance: float,
    equity_usd: float,
    limits: SizingLimits = DEFAULT_LIMITS,
    open_positions: int = 0,
    open_notional_usd: float = 0.0,
    free_margin_usd: float | None = None,
) -> SizingResult:
    """Size one entry, or explain why it cannot be sized.

    `stop_distance` is in price units and must be > 0 -- an ATR of zero on a
    flat synthetic series would otherwise divide by zero and produce an infinite
    position.
    """
    if price <= 0 or stop_distance <= 0 or equity_usd <= 0:
        return _zero(
            Reject.BAD_INPUT, f"price={price} stop_distance={stop_distance} equity={equity_usd}"
        )
    if open_positions >= limits.max_concurrent_positions:
        return _zero(
            Reject.MAX_POSITIONS, f"{open_positions}/{limits.max_concurrent_positions} already open"
        )

    ct_val = float(instrument.multiplier)
    risk_usd = equity_usd * limits.risk_equity_pct / 100.0

    # Risk per contract = stop distance in price * base units per contract.
    risk_per_contract = stop_distance * ct_val
    raw_contracts = risk_usd / risk_per_contract

    budget = equity_usd * limits.max_leverage
    per_asset_cap = budget * limits.per_asset_pct_of_budget / 100.0

    # Apply caps BEFORE rounding, so rounding is the last step and never
    # rounds a capped size back up over a limit.
    contracts = raw_contracts
    capped: list[str] = []

    remaining_budget = budget - open_notional_usd
    if remaining_budget <= 0:
        return _zero(
            Reject.LEVERAGE_CEILING,
            f"open notional ${open_notional_usd:.2f} already at "
            f"{limits.max_leverage}x ceiling (${budget:.2f})",
        )

    max_by_budget = remaining_budget / (price * ct_val)
    if contracts > max_by_budget:
        contracts = max_by_budget
        capped.append("leverage ceiling")

    max_by_asset = per_asset_cap / (price * ct_val)
    if contracts > max_by_asset:
        contracts = max_by_asset
        capped.append("per-asset cap")

    if free_margin_usd is not None:
        usable = free_margin_usd * limits.free_margin_buffer
        margin_per_contract = price * ct_val / limits.max_leverage
        max_by_margin = usable / margin_per_contract if margin_per_contract > 0 else 0.0
        if contracts > max_by_margin:
            contracts = max_by_margin
            capped.append("free margin")

    # R17: never round() an order quantity -- quantise to the instrument's own
    # size increment, DOWN, so a cap is never exceeded by the rounding itself.
    #
    # Quantised with Decimal rather than `instrument.make_qty(..., round_down=True)`
    # because make_qty RAISES ValueError when the value rounds to zero
    # ("was rounded to zero due to size increment"). At small equity that is the
    # normal, expected outcome and must be a clean rejection, not an exception.
    step = Decimal(str(instrument.size_increment))
    qty_dec = (Decimal(repr(contracts)) // step) * step

    if qty_dec <= 0 or qty_dec < Decimal(str(instrument.min_quantity)):
        return _zero(
            Reject.BELOW_MIN_QTY,
            f"sized {contracts:.8f} contracts, venue minimum is "
            f"{instrument.min_quantity} (need ~"
            f"${min_notional_usd(instrument, price) / (limits.risk_equity_pct / 100) * (stop_distance / price):.2f} "
            f"equity at this stop distance)",
        )

    notional = float(qty_dec) * ct_val * price
    floor = min_notional_usd(instrument, price) * limits.min_notional_headroom
    if notional < floor:
        # R6: reject, never widen the position to reach a venue minimum.
        return _zero(
            Reject.BELOW_MIN_NOTIONAL, f"notional ${notional:.2f} < venue minimum ${floor:.2f}"
        )

    # Round-trip through the instrument so the value is one nautilus accepts.
    qty_dec = Decimal(str(instrument.make_qty(qty_dec)))

    actual_risk = float(qty_dec) * ct_val * stop_distance
    eff_lev = (open_notional_usd + notional) / equity_usd

    return SizingResult(
        quantity=qty_dec,
        base_units=qty_dec * Decimal(str(ct_val)),
        notional_usd=notional,
        risk_usd=actual_risk,
        effective_leverage=eff_lev,
        detail=("capped by " + " + ".join(capped)) if capped else "",
    )
