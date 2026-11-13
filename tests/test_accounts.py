"""What the OKX keys can actually see, without a human logging into the exchange.

Two questions needed the OKX web app until now: how much USDT each sub-account
really holds, and whether the box's IPv6 address is on the key's IP whitelist.
The second one became urgent with F061 -- a signed call left over IPv6, OKX
refused it with 50110, our helper now pins IPv4, and the nautilus adapter's own
client cannot be pinned from config. So the address has to be whitelisted, and
"the owner says he whitelisted it" is not a measurement (R11).

Written before `ntx/ops/accounts.py` existed (R41).
"""

from __future__ import annotations

import json

from ntx.config.settings import Settings
from ntx.ops import accounts

KEYS = {
    "OKX_API_KEY": "LIVE-KEY",
    "OKX_API_SECRET": "LIVE-SECRET",
    "OKX_API_PASSPHRASE": "LIVE-PASS",
    "OKX_DEMO_API_KEY": "DEMO-KEY",
    "OKX_DEMO_API_SECRET": "DEMO-SECRET",
    "OKX_DEMO_API_PASSPHRASE": "DEMO-PASS",
}
ADDRESS = "2a01:4f9:c015:6148::1"


def _settings(**over) -> Settings:
    kw = {"NTX_VENUE": "okx_live", **KEYS}
    kw.update(over)
    return Settings(_env_file=None, **kw)


def _config_body(label="nautilus-live", lv="2", pos="net_mode", uid="700", main="100") -> dict:
    return {
        "code": "0",
        "data": [{"uid": uid, "mainUid": main, "label": label, "acctLv": lv, "posMode": pos}],
    }


def _balance_body(cash="100.04", eq="100.04") -> dict:
    return {
        "code": "0",
        "data": [
            {
                "details": [
                    {"ccy": "BTC", "cashBal": "0.001", "eq": "60"},
                    {"ccy": "USDT", "cashBal": cash, "eq": eq},
                ]
            }
        ],
    }


def _wire(monkeypatch, handler, settings=None, ipv6=ADDRESS):
    """Replace the venue and the IPv6 probe; return the calls that were made."""
    monkeypatch.setattr(accounts, "load", lambda: settings or _settings())
    monkeypatch.setattr(accounts, "ipv6_source_address", lambda *a, **k: ipv6)
    seen: list[dict] = []

    def fake_signed(method, base, path, creds, demo, body=None, family="ipv4"):
        seen.append({"path": path, "demo": demo, "family": family, "creds": creds})
        return handler(path, demo, family)

    monkeypatch.setattr(accounts, "signed", fake_signed)
    return seen


def _healthy(path, demo, family):
    if "config" in path:
        return _config_body(label="nautilus-demo" if demo else "nautilus-live")
    return _balance_body("4999.66", "4999.66") if demo else _balance_body()


# ------------------------------------------------------------- reading ----- #
def test_each_key_reports_its_own_account_and_its_own_balance(monkeypatch, capsys):
    _wire(monkeypatch, _healthy)
    assert accounts.main([]) == 0
    out = capsys.readouterr().out
    assert "nautilus-live" in out and "nautilus-demo" in out
    assert "100.04" in out and "4,999.66" in out
    assert "sub-account" in out  # both keys belong to sub-accounts (ADR-0014)


def test_live_only_never_touches_the_demo_key(monkeypatch, capsys):
    seen = _wire(monkeypatch, _healthy)
    assert accounts.main(["--live"]) == 0
    assert {c["demo"] for c in seen} == {False}
    assert "DEMO" not in capsys.readouterr().out


def test_a_venue_error_is_never_printed_as_a_zero_balance(monkeypatch, capsys):
    """R18/R31: an error body has no balance in it. Saying $0.00 here would read
    as 'the account was drained' during exactly the wrong five minutes."""
    _wire(monkeypatch, lambda p, d, f: {"code": "50119", "msg": "API key doesn't exist"})
    assert accounts.main(["--live"]) == 1
    cap = capsys.readouterr()
    text = cap.out + cap.err
    assert "50119" in text
    assert "0.00" not in text


def test_a_balance_that_cannot_be_read_does_not_claim_an_equity(monkeypatch, capsys):
    def handler(path, demo, family):
        return _config_body() if "config" in path else {"code": "50011", "msg": "rate limited"}

    _wire(monkeypatch, handler)
    assert accounts.main(["--live"]) == 1
    text = capsys.readouterr().out
    assert "50011" in text and "0.00" not in text


def test_spot_mode_is_flagged_because_every_order_would_be_refused(monkeypatch, capsys):
    """F055: acctLv=1 looked healthy for three hours of soak."""

    def handler(path, demo, family):
        return _config_body(lv="1") if "config" in path else _balance_body()

    _wire(monkeypatch, handler)
    assert accounts.main(["--live"]) == 1
    assert "acctLv=1" in capsys.readouterr().out


def test_no_keys_at_all_is_exit_2_not_a_clean_bill(monkeypatch, capsys):
    blank = _settings(**dict.fromkeys(KEYS, ""))
    _wire(monkeypatch, _healthy, settings=blank)
    assert accounts.main([]) == 2
    assert "no" in (capsys.readouterr().err.lower())


def test_a_dead_network_is_exit_2_not_exit_1(monkeypatch):
    """'I could not reach the venue' and 'the venue refused me' are opposite
    facts and must not share an exit code (R70)."""
    _wire(monkeypatch, lambda p, d, f: {"_err": "empty response"})
    assert accounts.main(["--live"]) == 2


# ---------------------------------------------------------- ipv6 (F061) ---- #
def test_ipv6_whitelisted_when_the_signed_call_succeeds(monkeypatch, capsys):
    seen = _wire(monkeypatch, _healthy)
    assert accounts.main(["--live", "--check-ipv6"]) == 0
    assert "IPv6 whitelisted (signed call succeeded over IPv6)" in capsys.readouterr().out
    assert any(c["family"] == "ipv6" for c in seen), seen


def test_ipv6_not_whitelisted_names_the_address_okx_saw(monkeypatch, capsys):
    def handler(path, demo, family):
        if family == "ipv6":
            return {
                "code": "50110",
                "msg": f"Your IP {ADDRESS} is not included in your API key's IP whitelist",
            }
        return _healthy(path, demo, family)

    _wire(monkeypatch, handler)
    assert accounts.main(["--live", "--check-ipv6"]) == 1
    out = capsys.readouterr().out
    assert "NOT whitelisted" in out and "50110" in out and ADDRESS in out


def test_no_ipv6_route_is_said_out_loud_and_is_not_a_failure(monkeypatch, capsys):
    """Nothing can leave over IPv6, so nothing can be refused for it. That is a
    fine state -- but it must be reported as 'no route', never as 'whitelisted'."""
    seen = _wire(monkeypatch, _healthy, ipv6=None)
    assert accounts.main(["--live", "--check-ipv6"]) == 0
    assert "no IPv6 route from this host" in capsys.readouterr().out
    assert not any(c["family"] == "ipv6" for c in seen)


def test_an_unexpected_ipv6_error_is_reported_raw_not_swallowed(monkeypatch, capsys):
    def handler(path, demo, family):
        if family == "ipv6":
            return {"code": "50102", "msg": "Timestamp request expired"}
        return _healthy(path, demo, family)

    _wire(monkeypatch, handler)
    assert accounts.main(["--live", "--check-ipv6"]) == 1
    assert "50102" in capsys.readouterr().out


# ------------------------------------------------------------ hygiene ------ #
def test_no_key_material_ever_reaches_the_output(monkeypatch, capsys):
    """R1. The tool prints balances and labels; it must never print a credential,
    and its output is pasted into chat by whoever runs it on the box."""
    for argv in ([], ["--json"], ["--check-ipv6"], ["--json", "--check-ipv6"]):
        _wire(monkeypatch, _healthy)
        accounts.main(argv)
        cap = capsys.readouterr()
        text = cap.out + cap.err
        for secret in KEYS.values():
            assert secret not in text, f"{secret} leaked with {argv}"


def test_json_carries_the_same_facts_and_parses(monkeypatch, capsys):
    _wire(monkeypatch, _healthy)
    assert accounts.main(["--live", "--json", "--check-ipv6"]) == 0
    payload = json.loads(capsys.readouterr().out)
    (row,) = payload["accounts"]
    assert row["env"] == "live"
    assert row["usdt_equity"] == 100.04
    assert row["authenticated"] is True
    assert payload["ipv6"]["live"]["status"] == "whitelisted"


def test_the_usdt_row_is_picked_out_of_the_other_currencies():
    assert accounts.usdt_cash(_balance_body("12.5", "13.0")) == 12.5


def test_a_venue_message_that_echoes_a_credential_is_scrubbed(monkeypatch, capsys):
    """R1. Venue error text is printed verbatim, and this output gets pasted into
    chat by whoever ran it on the box. OKX does not echo keys today; "today" is
    not a guarantee worth a leaked credential."""
    _wire(monkeypatch, lambda p, d, f: {"code": "50113", "msg": "Invalid sign for LIVE-KEY"})
    assert accounts.main(["--live"]) == 1
    text = capsys.readouterr().out
    assert "LIVE-KEY" not in text
    assert "50113" in text and "redacted" in text
