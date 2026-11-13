"""Startup feasibility gate.

At small equity, exchange minimums bind long before risk limits do. Without this
gate the node starts, subscribes, generates signals, and has **every single order
rejected** by the venue -- while logs fill and nothing announces that no trading
is possible. That is failure pattern 13/15 in small-account form.

So: before subscribing to anything, compute the position this account would
actually take and drop every instrument where it falls below the venue minimum.
If nothing survives, refuse to start with an error that states the equity needed.
"""

from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.model.instruments import Instrument

from ntx.risk.sizing import DEFAULT_LIMITS, Reject, SizingLimits, size_position


class NoTradableInstruments(RuntimeError):
    """Raised instead of starting a node that cannot place a single order."""


@dataclass(frozen=True)
class Feasibility:
    instrument_id: str
    tradable: bool
    price: float
    min_notional_usd: float
    sized_notional_usd: float
    sized_qty: float
    reject: Reject
    reason: str = ""
    equity_needed_usd: float | None = None

    def line(self) -> str:
        if self.tradable:
            return (
                f"  KEEP {self.instrument_id:<18} qty={self.sized_qty:<10g} "
                f"notional=${self.sized_notional_usd:>8.2f} "
                f"(min ${self.min_notional_usd:.2f})"
            )
        need = f", needs ~${self.equity_needed_usd:,.0f} equity" if self.equity_needed_usd else ""
        return f"  DROP {self.instrument_id:<18} {self.reject}: {self.reason}{need}"


def _equity_needed(min_notional: float, stop_frac: float, risk_pct: float) -> float:
    """Equity at which this instrument becomes tradable at the configured risk.

    notional = (equity * risk_pct) / stop_frac  =>  equity = notional * stop_frac / risk_pct
    """
    if risk_pct <= 0 or stop_frac <= 0:
        return float("inf")
    return min_notional * stop_frac / (risk_pct / 100.0)


def assess(
    *,
    instruments: dict[str, Instrument],
    prices: dict[str, float],
    equity_usd: float,
    typical_stop_price_pct: float = 4.0,
    limits: SizingLimits = DEFAULT_LIMITS,
) -> list[Feasibility]:
    """Assess every instrument at the configured risk and a typical stop width.

    `typical_stop_price_pct` approximates `sl_atr_mult * ATR` as a fraction of
    price. It is only used for feasibility screening -- live sizing always uses
    the real ATR.
    """
    out: list[Feasibility] = []
    for inst_id, inst in sorted(instruments.items()):
        price = prices.get(inst_id, 0.0)
        if price <= 0:
            out.append(
                Feasibility(
                    inst_id, False, 0.0, 0.0, 0.0, 0.0, Reject.BAD_INPUT, "no price available"
                )
            )
            continue

        stop_distance = price * typical_stop_price_pct / 100.0
        min_notional = float(inst.min_quantity) * float(inst.multiplier) * price
        r = size_position(
            instrument=inst,
            price=price,
            stop_distance=stop_distance,
            equity_usd=equity_usd,
            limits=limits,
        )
        out.append(
            Feasibility(
                instrument_id=inst_id,
                tradable=r.ok,
                price=price,
                min_notional_usd=min_notional,
                sized_notional_usd=r.notional_usd,
                sized_qty=float(r.quantity),
                reject=r.reject,
                reason=r.detail if not r.ok else "",
                equity_needed_usd=(
                    None
                    if r.ok
                    else _equity_needed(
                        min_notional, typical_stop_price_pct / 100.0, limits.risk_equity_pct
                    )
                ),
            )
        )
    return out


def tradable_universe(
    *,
    instruments: dict[str, Instrument],
    prices: dict[str, float],
    equity_usd: float,
    typical_stop_price_pct: float = 4.0,
    limits: SizingLimits = DEFAULT_LIMITS,
    strict: bool = True,
) -> tuple[list[str], list[Feasibility]]:
    """Return (tradable instrument ids, full assessment).

    With `strict`, an empty result raises rather than starting a node that
    cannot trade. Fail closed.
    """
    results = assess(
        instruments=instruments,
        prices=prices,
        equity_usd=equity_usd,
        typical_stop_price_pct=typical_stop_price_pct,
        limits=limits,
    )
    keep = [f.instrument_id for f in results if f.tradable]
    if not keep and strict:
        cheapest = min(
            (f for f in results if f.equity_needed_usd not in (None, float("inf"))),
            key=lambda f: f.equity_needed_usd or float("inf"),
            default=None,
        )
        hint = (
            f" Cheapest instrument ({cheapest.instrument_id}) needs about "
            f"${cheapest.equity_needed_usd:,.2f}."
            if cheapest
            else ""
        )
        raise NoTradableInstruments(
            f"No instrument is tradable at ${equity_usd:,.2f} equity with "
            f"risk_equity_pct={limits.risk_equity_pct} and a "
            f"{typical_stop_price_pct}% stop.{hint}"
        )
    return keep, results


def report(results: list[Feasibility], equity_usd: float) -> str:
    keep = [f for f in results if f.tradable]
    drop = [f for f in results if not f.tradable]
    lines = [f"preflight @ ${equity_usd:,.2f} equity: {len(keep)} tradable, {len(drop)} dropped"]
    lines += [f.line() for f in results]
    return "\n".join(lines)
