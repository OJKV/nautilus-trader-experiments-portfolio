"""Build nautilus instruments from LIVE OKX specs.

Price series come from Binance; **instrument definitions come from OKX** (ADR-0005).
That split is deliberate: the backtest must use the tick size, contract value,
minimum size and fee rates of the venue we actually trade, or its fills and its
min-notional rejections will not match live.

OKX public endpoints need no tunnel from Kazakhstan (`my.okx.com`), so this runs
without the VPN.

OKX sizes swaps in **contracts**: one contract is `ct_val` units of the base
currency. So an order quantity of 1 means `ct_val` base units, and
`notional = qty * ct_val * price`. The `multiplier` field carries `ct_val`.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from decimal import Decimal
from pathlib import Path

from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Currency, Price, Quantity

from ntx.config.universe import UNIVERSE
from ntx.config.venues import BROWSER_UA
from ntx.obs.atomic import atomic_write_json, safe_read_json

log = logging.getLogger(__name__)

OKX_VENUE = Venue("OKX")

# Measured from the venue 2026-09-15 (Lv1). Refreshed at live startup from
# /api/v5/account/trade-fee -- never hardcoded as the source of truth (R16).
# Negative in OKX's API means "you pay"; nautilus wants a positive cost.
DEFAULT_MAKER_FEE = Decimal("0.0002")
DEFAULT_TAKER_FEE = Decimal("0.0005")


def _decimals(increment: str) -> int:
    """Decimal places implied by a tick/lot string such as '0.0001'."""
    d = Decimal(increment).normalize()
    exp = d.as_tuple().exponent
    return max(0, -int(exp))


def fetch_okx_swap_specs(public_base_url: str = "https://my.okx.com") -> dict[str, dict]:
    """Fetch OKX SWAP instrument specs. Public endpoint, no credentials."""
    url = f"{public_base_url.rstrip('/')}/api/v5/public/instruments?instType=SWAP"
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})  # R25
    with urllib.request.urlopen(req, timeout=40) as r:
        payload = json.loads(r.read())
    if payload.get("code") != "0":
        raise RuntimeError(
            f"OKX instruments error: code={payload.get('code')} {payload.get('msg')}"
        )
    return {i["instId"]: i for i in payload["data"]}


def load_specs(cache_path: Path, public_base_url: str, *, refresh: bool = False) -> dict[str, dict]:
    """Return OKX specs, using an on-disk cache so backtests are reproducible.

    Specs change (tick sizes and max leverage do get revised), so a cached copy
    is what makes a backtest re-runnable with the same assumptions. The cache is
    written atomically.
    """
    if not refresh:
        cached = safe_read_json(cache_path)
        if cached:
            return cached
    specs = fetch_okx_swap_specs(public_base_url)
    wanted = {a.okx_inst_id: specs[a.okx_inst_id] for a in UNIVERSE if a.okx_inst_id in specs}
    missing = [a.okx_inst_id for a in UNIVERSE if a.okx_inst_id not in specs]
    if missing:
        raise RuntimeError(f"OKX no longer lists: {missing}")
    atomic_write_json(cache_path, wanted)
    return wanted


def build_instrument(
    spec: dict,
    *,
    maker_fee: Decimal = DEFAULT_MAKER_FEE,
    taker_fee: Decimal = DEFAULT_TAKER_FEE,
    ts: int = 0,
) -> CryptoPerpetual:
    """Turn one OKX spec dict into a nautilus `CryptoPerpetual`."""
    inst_id = InstrumentId(Symbol(spec["instId"]), OKX_VENUE)
    base = Currency.from_str(spec["ctValCcy"] if spec["ctValCcy"] != "USDT" else "USDT")
    tick = spec["tickSz"]
    lot = spec["lotSz"]
    return CryptoPerpetual(
        instrument_id=inst_id,
        raw_symbol=Symbol(spec["instId"]),
        base_currency=base,
        quote_currency=USDT,
        settlement_currency=USDT,
        is_inverse=False,
        price_precision=_decimals(tick),
        size_precision=_decimals(lot),
        price_increment=Price.from_str(tick),
        size_increment=Quantity.from_str(lot),
        # OKX quantities are CONTRACTS; ct_val base units per contract.
        multiplier=Quantity.from_str(spec["ctVal"]),
        min_quantity=Quantity.from_str(spec["minSz"]),
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        margin_init=Decimal("0.05"),
        margin_maint=Decimal("0.025"),
        ts_event=ts,
        ts_init=ts,
        info=spec,
    )


def build_all(
    cache_path: Path, public_base_url: str, *, refresh: bool = False
) -> dict[str, CryptoPerpetual]:
    specs = load_specs(cache_path, public_base_url, refresh=refresh)
    return {inst_id: build_instrument(s) for inst_id, s in specs.items()}


def min_notional_usd(inst: CryptoPerpetual, price: float) -> float:
    """Smallest order value OKX accepts: min_qty * ct_val * price."""
    return float(inst.min_quantity) * float(inst.multiplier) * price


def asset_class_of(inst: CryptoPerpetual) -> AssetClass:
    return inst.asset_class
