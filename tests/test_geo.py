"""VPN egress-country guard.

Proton VPN Free auto-assigns one of ten countries, and free users cannot pick.
Three of those -- Canada, Japan, United States -- are OKX-restricted. Sending
authenticated OKX calls from a restricted jurisdiction risks the account, so the
exit country is checked BEFORE any authenticated call. Written before the
implementation (R41).
"""

from __future__ import annotations

import pytest

from ntx.config import geo


@pytest.mark.parametrize(
    "cc",
    [
        "US",
        "CA",
        "JP",
        "HK",
        "IN",
        "MY",
        "UZ",
        "NG",
        "GB",
        "AU",
        "BR",
        "KR",
        "KZ",
        "PR",
        "IR",
        "KP",
    ],
)
def test_restricted_countries_are_blocked(cc):
    verdict = geo.assess(cc)
    assert not verdict.allowed, f"{cc} must be blocked"
    assert verdict.reason


@pytest.mark.parametrize("cc", ["SG", "CH", "MX", "TR", "AE", "VN", "TW"])
def test_clean_countries_are_allowed(cc):
    v = geo.assess(cc)
    assert v.allowed and not v.eea


@pytest.mark.parametrize("cc", ["NL", "NO", "PL", "RO", "DE", "FI"])
def test_eea_countries_are_allowed_with_a_warning(cc):
    """EEA exits worked empirically for this GLOBAL account (NL, 2026-09-15), but
    carry an entity-routing risk, so they pass with a caveat rather than silently."""
    v = geo.assess(cc)
    assert v.allowed and v.eea
    assert "EEA" in v.reason


def test_proton_free_pool_is_fully_classified():
    """Every country Proton Free can hand you must have a known verdict."""
    verdicts = {cc: geo.assess(cc) for cc in geo.PROTON_FREE_COUNTRIES}
    blocked = sorted(cc for cc, v in verdicts.items() if not v.allowed)
    assert blocked == ["CA", "JP", "US"]
    assert len(geo.PROTON_FREE_COUNTRIES) == 10


def test_kazakhstan_is_blocked_so_no_vpn_is_detected():
    v = geo.assess("KZ")
    assert not v.allowed
    assert "VPN" in v.reason


def test_unknown_and_malformed_codes_fail_closed():
    for bad in ("", None, "XX1", "zz"):
        v = geo.assess(bad)
        assert not v.allowed, f"{bad!r} must fail closed"


def test_lowercase_is_normalised():
    assert geo.assess("sg").allowed
    assert not geo.assess("jp").allowed


def test_parse_cloudflare_trace():
    body = "fl=1\nh=www.cloudflare.com\nip=203.0.113.9\nloc=SG\ncolo=SIN\n"
    assert geo.parse_trace(body) == ("SG", "SIN")
    assert geo.parse_trace("garbage") == (None, None)


def test_check_egress_uses_the_fetcher_and_never_raises():
    ok = geo.check_egress(fetch=lambda: "loc=CH\ncolo=ZRH\n")
    assert ok.allowed and ok.country == "CH"

    blocked = geo.check_egress(fetch=lambda: "loc=JP\ncolo=NRT\n")
    assert not blocked.allowed and blocked.country == "JP"

    def boom():
        raise OSError("network down")

    failed = geo.check_egress(fetch=boom)
    assert not failed.allowed, "an unknown egress must fail closed"


@pytest.mark.parametrize("cc", ["XX", "T1", "ZZ", "AA", "QM", "QZ", "XA", "XK", "XZ"])
def test_cloudflare_special_and_private_use_codes_fail_closed(cc):
    """Cloudflare returns XX for an unlocatable client and T1 for Tor. Both look
    like country codes and identify no jurisdiction we can vouch for."""
    assert not geo.assess(cc).allowed, f"{cc} must fail closed"


@pytest.mark.parametrize(
    ("cc", "expect_hint", "expect_code"),
    [
        ("JP", True, 2),  # Proton-assignable AND blocked -> tell them to switch
        ("SG", False, 0),  # Proton-assignable but fine -> no nagging
        ("KZ", False, 2),  # blocked but not a Proton server -> VPN is simply off
    ],
)
def test_main_shows_proton_hint_only_when_it_helps(
    monkeypatch, capsys, cc, expect_hint, expect_code
):
    """Mutation survivor (`and` -> `or`): advice shown when it does not apply
    trains people to ignore it when it does."""
    monkeypatch.setattr(geo, "check_egress", lambda: geo.assess(cc, "TST"))
    assert geo.main() == expect_code
    out = capsys.readouterr().out
    assert ("Change server" in out) is expect_hint
