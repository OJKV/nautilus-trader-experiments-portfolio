"""Where is our traffic actually coming from, and is OKX allowed there?

Checked BEFORE any authenticated OKX call. The motivating case: Proton VPN Free
auto-assigns one of ten countries and free users cannot choose -- and three of
those (Canada, Japan, United States) are OKX-restricted. Connecting from a
restricted jurisdiction risks the account, so it fails closed.

Egress is read from Cloudflare's public trace endpoint, which reports the country
any website already sees. No personal data is sent.

The restricted list reflects OKX's published restrictions as researched
2026-09-15 (docs/07 §5). It is not authoritative -- OKX's own Risk & Compliance
Disclosure is -- so keep it under review.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from ntx.config.venues import BROWSER_UA

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

# Fully prohibited, incl. US territories.
PROHIBITED = {
    "US",
    "PR",
    "GU",
    "AS",
    "MP",
    "VI",  # United States + territories
    "CA",
    "HK",
    "IN",
    "JP",
    "MY",
    "UZ",
    "NG",
}
# Comprehensive sanctions. Occupied Ukrainian regions cannot be detected from a
# country code (they report UA), which is a known gap.
SANCTIONED = {"AF", "CU", "IR", "KP", "SY"}
NATIONAL_BAN = {"DZ", "BD", "NP"}
# Spot allowed, derivatives blocked -- and we trade perpetual swaps.
NO_DERIVATIVES = {"AU", "BR", "KR", "GB"}
# The user's home jurisdiction: authenticated OKX is blocked here.
HOME_BLOCKED = {"KZ"}

# EU + EEA. Worked empirically for this GLOBAL account (NL exit, 2026-09-15), but
# an EEA IP may route toward OKX's EEA entity -- allowed, with a caveat.
EEA = {
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
    "IS",
    "LI",
    "NO",
}

# What Proton VPN Free can assign (no manual choice on the free tier).
PROTON_FREE_COUNTRIES = ("CA", "JP", "MX", "NL", "NO", "PL", "RO", "SG", "CH", "US")


@dataclass(frozen=True)
class Verdict:
    country: str | None
    allowed: bool
    eea: bool
    reason: str
    colo: str | None = None


# Codes that look valid but identify no real jurisdiction. Cloudflare returns
# "XX" when it cannot locate a client and "T1" for Tor; ISO reserves AA, QM-QZ,
# XA-XZ and ZZ for private use. Any of these must fail CLOSED -- treating an
# unrecognised code as "not on the restricted list" would wave through exactly
# the traffic we cannot vouch for.
_SPECIAL = {"XX", "T1", "AA", "ZZ"}
_PRIVATE_RANGES = (("QM", "QZ"), ("XA", "XZ"))


def _is_private_or_special(cc: str) -> bool:
    return cc in _SPECIAL or any(lo <= cc <= hi for lo, hi in _PRIVATE_RANGES)


def _normalise(cc: object) -> str | None:
    if not isinstance(cc, str):
        return None
    cc = cc.strip().upper()
    if len(cc) != 2 or not cc.isalnum() or _is_private_or_special(cc):
        return None
    return cc if cc.isalpha() else None


def assess(country_code: object, colo: str | None = None) -> Verdict:
    """Classify an ISO-3166 alpha-2 code. Unknown or malformed fails closed."""
    cc = _normalise(country_code)
    if cc is None:
        return Verdict(
            None,
            False,
            False,
            f"cannot determine egress country from {country_code!r} -- failing closed",
            colo,
        )
    if cc in HOME_BLOCKED:
        return Verdict(
            cc,
            False,
            False,
            "egress is Kazakhstan: the VPN is OFF or not routing -- authenticated OKX will fail",
            colo,
        )
    for group, why in (
        (PROHIBITED, "OKX-prohibited jurisdiction"),
        (SANCTIONED, "sanctioned jurisdiction"),
        (NATIONAL_BAN, "national crypto ban"),
        (NO_DERIVATIVES, "OKX blocks derivatives here; we trade perps"),
    ):
        if cc in group:
            return Verdict(
                cc, False, False, f"{cc}: {why}. Switch VPN server before any OKX call", colo
            )
    if cc in EEA:
        return Verdict(
            cc,
            True,
            True,
            f"{cc} is EEA: worked for this GLOBAL account, but may route "
            f"toward OKX's EEA entity. Prefer SG/CH/MX if available",
            colo,
        )
    return Verdict(cc, True, False, f"{cc}: not on OKX's restricted list", colo)


def parse_trace(body: str) -> tuple[str | None, str | None]:
    loc = colo = None
    for line in body.splitlines():
        key, _, value = line.partition("=")
        if key == "loc":
            loc = value.strip() or None
        elif key == "colo":
            colo = value.strip() or None
    return loc, colo


def _fetch_trace(timeout: int = 8) -> str:
    req = urllib.request.Request(TRACE_URL, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2048).decode(errors="replace")


def check_egress(fetch: Callable[[], str] | None = None) -> Verdict:
    """Determine the live egress country. Never raises; unknown fails closed."""
    try:
        body = (fetch or _fetch_trace)()
    except Exception as e:
        return Verdict(
            None, False, False, f"egress check failed ({type(e).__name__}) -- failing closed"
        )
    loc, colo = parse_trace(body)
    return assess(loc, colo)


def main() -> int:
    v = check_egress()
    mark = "OK  " if v.allowed else "STOP"
    print(f"[{mark}] egress country={v.country} colo={v.colo}")
    print(f"       {v.reason}")
    if v.country in PROTON_FREE_COUNTRIES and not v.allowed:
        print("       Proton Free: click 'Change server' and re-run `make vpn-check`.")
    return 0 if v.allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
