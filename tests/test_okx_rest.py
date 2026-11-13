"""Signed OKX requests must not put the key or passphrase in the process list.

Review L-14: `/proc/<pid>/cmdline` is world-readable, and this VPS is shared with
another project running as a different user. The HMAC signature is harmless --
it is single-use and scoped to one request -- but the API key and the passphrase
are long-lived credentials, and two of the three factors is two too many.

Written before the fix (R41).
"""

from __future__ import annotations

import pytest

from ntx.ops import okx_rest


def _values(config: str, want: str) -> list[str]:
    """The values curl will actually use, un-escaped again."""
    out = []
    for line in config.splitlines():
        keyword, _, raw = line.partition(" = ")
        if keyword != want:
            continue
        inner = raw[1:-1] if raw.startswith('"') else raw
        out.append(inner.replace('\\"', '"').replace("\\\\", "\\"))
    return out


KEY, SECRET, PASSPHRASE = "THE-API-KEY", "THE-SECRET", "THE-PASSPHRASE"
CREDS = (KEY, SECRET, PASSPHRASE)


def _capture(monkeypatch):
    seen = {}

    class _Done:
        stdout = '{"code":"0","data":[]}'
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["input"] = kw.get("input") or ""
        return _Done()

    monkeypatch.setattr(okx_rest.subprocess, "run", fake_run)
    return seen


def test_no_credential_is_visible_in_the_argv(monkeypatch):
    seen = _capture(monkeypatch)
    okx_rest.signed("GET", "https://www.okx.com", "/api/v5/account/balance", CREDS, demo=False)
    argv = " ".join(seen["cmd"])
    for secret in (KEY, SECRET, PASSPHRASE):
        assert secret not in argv, f"{secret} is readable in /proc/<pid>/cmdline"


def test_the_credentials_travel_on_stdin_instead(monkeypatch):
    seen = _capture(monkeypatch)
    okx_rest.signed("GET", "https://www.okx.com", "/api/v5/account/balance", CREDS, demo=False)
    assert KEY in seen["input"] and PASSPHRASE in seen["input"]
    assert "--config" in seen["cmd"] and "-" in seen["cmd"]
    # The secret itself is never sent anywhere, only the HMAC it produces.
    assert SECRET not in seen["input"] and SECRET not in " ".join(seen["cmd"])


def test_a_post_sends_the_exact_signed_body(monkeypatch):
    """The HMAC covers the literal bytes, so the body must not be reformatted."""
    seen = _capture(monkeypatch)
    okx_rest.signed(
        "POST",
        "https://www.okx.com",
        "/api/v5/account/set-leverage",
        CREDS,
        demo=True,
        body={"instId": "SOL-USDT-SWAP", "lever": "3"},
    )
    assert _values(seen["input"], "data") == ['{"instId":"SOL-USDT-SWAP","lever":"3"}']
    assert "x-simulated-trading: 1" in _values(seen["input"], "header")


def test_quotes_and_backslashes_in_a_passphrase_do_not_break_the_config(monkeypatch):
    """A curl config file is quoted text; an unescaped `"` would silently corrupt
    the header and produce a baffling auth failure."""
    seen = _capture(monkeypatch)
    nasty = ('k"ey', "sec", 'pass"phrase\\here')
    okx_rest.signed("GET", "https://www.okx.com", "/api/v5/account/balance", nasty, demo=False)
    headers = _values(seen["input"], "header")
    assert 'OK-ACCESS-KEY: k"ey' in headers, headers
    assert 'OK-ACCESS-PASSPHRASE: pass"phrase\\here' in headers, headers


def test_an_error_body_is_returned_not_raised(monkeypatch):
    seen = _capture(monkeypatch)
    assert isinstance(okx_rest.signed("GET", "https://www.okx.com", "/x", CREDS, demo=False), dict)
    assert seen["cmd"][0] == "curl"


def test_the_old_helpers_now_delegate():
    """`doctor._signed_get` and `flatten._signed_post` are monkeypatched by many
    tests and used by every ops tool; they keep their names and gain the fix."""
    import inspect

    from ntx.doctor import _signed_get
    from ntx.ops.flatten import _signed_post

    for fn in (_signed_get, _signed_post):
        assert "okx_rest" in inspect.getsource(fn), fn.__name__


def test_every_signed_call_goes_out_over_ipv4():
    """OKX keys are bound to the box's IPv4 (docs/21). The box also has an IPv6
    address, and `/etc/gai.conf` only *prefers* IPv4 -- on 2026-09-23 21:24 UTC a
    balance read went out over IPv6 and OKX answered

        50110 Your IP 2a01:4f9:...:1 is not included in your API key's IP whitelist

    Once in three days for a balance read is a shrug; the same helper carries the
    flatten kill switch, the stop release on restart and the leverage check, where
    "the venue refused because we came from the wrong address" is not a shrug.
    """
    cfg = okx_rest._config("https://www.okx.com/api/v5/account/balance", "GET", {}, None)
    assert "ipv4" in cfg.splitlines(), cfg


# ------------------------------------------------- address family (F061) ---- #
# Pinning IPv4 fixed the accident, but it also made the whitelist unverifiable:
# there was no way to ask "would a call from this box's IPv6 address be accepted
# now?" without a human reading the OKX web app. `family=` reopens that one door,
# deliberately, for the read-only check in `ntx.ops.accounts`.
#
# Written before the parameter existed (R41).


def test_a_call_can_be_forced_over_ipv6():
    cfg = okx_rest._config("https://www.okx.com/x", "GET", {}, None, family="ipv6")
    lines = cfg.splitlines()
    assert "ipv6" in lines and "ipv4" not in lines, cfg


def test_auto_pins_no_family_at_all():
    """`auto` is what curl does with no flag: whatever the resolver prefers."""
    lines = okx_rest._config("https://www.okx.com/x", "GET", {}, None, family="auto").splitlines()
    assert "ipv4" not in lines and "ipv6" not in lines, lines


def test_signed_defaults_to_ipv4_and_passes_the_family_through(monkeypatch):
    seen = _capture(monkeypatch)
    okx_rest.signed("GET", "https://www.okx.com", "/x", CREDS, demo=False)
    assert "ipv4" in seen["input"].splitlines()
    okx_rest.signed("GET", "https://www.okx.com", "/x", CREDS, demo=False, family="ipv6")
    assert "ipv6" in seen["input"].splitlines()


def test_an_unknown_family_is_refused_rather_than_ignored(monkeypatch):
    """A typo must not silently fall back to the resolver's choice -- that is the
    exact failure mode F061 was."""
    _capture(monkeypatch)
    with pytest.raises(ValueError, match="family"):
        okx_rest.signed("GET", "https://www.okx.com", "/x", CREDS, demo=False, family="IPv6")
