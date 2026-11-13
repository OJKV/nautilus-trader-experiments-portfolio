"""Venue registry.

Single source of truth for hosts, credentials, fee sources and capability flags.
Strategy and risk code must never branch on venue name -- it reads capabilities
from here instead (see docs/01-architecture.md §7, docs/14-extensibility.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# OKX's Cloudflare returns HTTP 403 with body "error code: 1010" for default
# library User-Agents (Python-urllib confirmed; reqwest unverified -- see Q6).
# 1010 is a banned-client-fingerprint response, NOT a geo-block. See F006.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)


class Venue(StrEnum):
    OKX = "OKX"
    BINANCE = "BINANCE"  # designed, not implemented (ADR-0010)
    BYBIT = "BYBIT"  # designed, not implemented (ADR-0010)


@dataclass(frozen=True)
class HostRole:
    """An OKX hostname and what it is actually good for.

    Measured from Kazakhstan 2026-09-15:
      - www.okx.com  : TCP-blocked without a VPN (code 000, User-Agent independent)
      - my./app./eea.: reachable, serve IDENTICAL public market data (same 479
        instruments, same last price AND same exchange ts), but return
        401 code=50119 "API key doesn't exist" for /api/v5/account/* -- they are
        CDN front doors that do not route the private account namespace.
    Therefore: public data needs no tunnel; anything authenticated does.
    """

    url: str
    public_data: bool
    authenticated: bool
    reachable_from_kz_without_vpn: bool
    note: str = ""


OKX_HOSTS: tuple[HostRole, ...] = (
    HostRole("https://www.okx.com", True, True, False, "canonical; needs VPN from KZ"),
    HostRole("https://my.okx.com", True, False, True, "public data only (50119 on private)"),
    HostRole("https://app.okx.com", True, False, True, "public data only (50119 on private)"),
    HostRole(
        "https://eea.okx.com", True, False, True, "public data only; never use for an account"
    ),
)


@dataclass(frozen=True)
class VenueSpec:
    venue: Venue
    implemented: bool

    # hosts
    auth_base_url: str
    public_base_url: str

    # credentials: env var names only -- values are never stored in code
    env_key: str
    env_secret: str
    env_passphrase: str | None

    # capability flags that strategy/risk code reads instead of branching on name
    native_trailing_stop: bool
    attaches_stop_at_entry: bool  # bracket/OCO support in the execution client
    supports_short: bool
    has_funding: bool
    is_24_7: bool

    # execution
    user_agent: str = BROWSER_UA
    fee_endpoint: str = ""  # fees are FETCHED, never hardcoded (R16)
    notes: tuple[str, ...] = field(default_factory=tuple)


OKX = VenueSpec(
    venue=Venue.OKX,
    implemented=True,
    auth_base_url="https://www.okx.com",
    public_base_url="https://my.okx.com",
    env_key="OKX_API_KEY",
    env_secret="OKX_API_SECRET",
    env_passphrase="OKX_API_PASSPHRASE",
    native_trailing_stop=True,
    # OKXExecutionClient submits neither bracket nor OCO orders, so OKX's
    # venue-side attachAlgoOrds is unreachable through the adapter. Protection is
    # therefore fill-then-attach-immediately + watchdog. See F007.
    attaches_stop_at_entry=False,
    supports_short=True,
    has_funding=True,
    is_24_7=True,
    fee_endpoint="/api/v5/account/trade-fee?instType=SWAP",
    notes=(
        "Account must be acctLv=2 (Single-currency margin) and posMode=net_mode.",
        "Non-IP-bound trade keys are auto-deleted after 14 idle days.",
        "Demo trading uses separate keys plus header x-simulated-trading: 1.",
    ),
)

REGISTRY: dict[Venue, VenueSpec] = {Venue.OKX: OKX}


def get(venue: Venue | str) -> VenueSpec:
    v = Venue(venue)
    spec = REGISTRY.get(v)
    if spec is None:
        raise NotImplementedError(
            f"{v} is designed but not implemented for MVP (ADR-0010). "
            f"Implemented: {[k.value for k in REGISTRY]}"
        )
    return spec


def min_notional_usd(
    *, price: float, min_size: float, ct_val: float = 1.0, venue_min_notional: float = 0.0
) -> float:
    """Smallest order value the venue will accept, in quote currency.

    OKX sizes in contracts: notional = min_size * ct_val * price.
    Binance/Bybit size in base units with a separate notional floor, hence the max().
    Always computed from LIVE specs -- these values move with price (R: preflight).
    """
    return max(min_size * ct_val * price, venue_min_notional)
