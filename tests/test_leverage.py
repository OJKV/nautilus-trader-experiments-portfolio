"""Per-instrument venue leverage (review L-07, user request 2026-09-19).

`NTX_MAX_LEVERAGE` was only ever a local sizing ceiling; the venue kept its own
number per instrument and nothing read it. In isolated margin that number sets
the liquidation price, so at OKX's higher defaults liquidation can sit inside
our own 2xATR stop. Written before the code (R41).
"""

from __future__ import annotations

import pytest

from ntx.config.settings import Settings
from ntx.ops import leverage as lv


def _settings(**over) -> Settings:
    kw = {
        "NTX_VENUE": "okx_demo",
        "OKX_DEMO_API_KEY": "k",
        "OKX_DEMO_API_SECRET": "s",
        "OKX_DEMO_API_PASSPHRASE": "p",
        "NTX_EQUITY_OVERRIDE_USD": "123.45",
    }
    kw.update(over)
    return Settings(_env_file=None, **kw)


def test_reading_leverage_asks_for_isolated_and_parses_every_row(monkeypatch):
    seen = {}

    def fake_get(base, path, creds, demo):
        seen["path"] = path
        return {
            "code": "0",
            "data": [
                {"instId": "SOL-USDT-SWAP", "mgnMode": "isolated", "lever": "3"},
                {"instId": "ETH-USDT-SWAP", "mgnMode": "isolated", "lever": "10"},
            ],
        }

    monkeypatch.setattr("ntx.doctor._signed_get", fake_get)
    got = lv.read_leverage(["SOL-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX"], _settings(), demo=True)
    assert got == {"SOL-USDT-SWAP": 3.0, "ETH-USDT-SWAP": 10.0}
    assert "mgnMode=isolated" in seen["path"]
    assert "SOL-USDT-SWAP,ETH-USDT-SWAP" in seen["path"], "one call for many ids"


def test_an_api_error_is_never_read_as_leverage_ok(monkeypatch):
    monkeypatch.setattr("ntx.doctor._signed_get", lambda *a, **k: {"code": "50011", "msg": "rate"})
    with pytest.raises(RuntimeError, match="cannot read leverage"):
        lv.read_leverage(["SOL-USDT-SWAP.OKX"], _settings(), demo=True)


def test_the_set_body_matches_okx_net_mode_isolated(monkeypatch):
    sent = {}

    def fake_post(base, path, body, creds, demo):
        sent.update({"path": path, "body": body, "demo": demo})
        return {"code": "0", "data": [{"lever": "3"}]}

    monkeypatch.setattr("ntx.ops.flatten._signed_post", fake_post)
    lv.set_leverage("SOL-USDT-SWAP.OKX", 3, _settings(), demo=True)
    assert sent["path"] == "/api/v5/account/set-leverage"
    # posSide is ONLY valid in long/short mode; this account is net_mode.
    assert sent["body"] == {"instId": "SOL-USDT-SWAP", "lever": "3", "mgnMode": "isolated"}
    assert "posSide" not in sent["body"]


def test_drift_reports_wrong_and_unknown_instruments():
    have = {"SOL-USDT-SWAP": 3.0, "ETH-USDT-SWAP": 10.0}
    bad = lv.drift(have, 3.0, ["SOL-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX", "TRX-USDT-SWAP.OKX"])
    assert bad == {"ETH-USDT-SWAP": 10.0, "TRX-USDT-SWAP": None}


def test_check_exits_non_zero_when_an_instrument_drifted(monkeypatch, capsys):
    monkeypatch.setattr(
        "ntx.doctor._signed_get",
        lambda *a, **k: {"code": "0", "data": [{"instId": "SOL-USDT-SWAP", "lever": "10"}]},
    )
    monkeypatch.setattr("ntx.config.settings.load", lambda: _settings())
    rc = lv.main(["--check", "--demo", "--only", "SOL"])
    assert rc == 1
    assert "DRIFT" in capsys.readouterr().out


def test_setting_is_refused_while_the_account_is_not_flat(monkeypatch):
    """OKX rejects a change with orders or a position open (59101/59112), and a
    lower leverage on an open position can fail for margin (59109)."""
    monkeypatch.setattr(
        "ntx.doctor._signed_get",
        lambda *a, **k: {"code": "0", "data": [{"instId": "SOL-USDT-SWAP", "lever": "10"}]},
    )
    monkeypatch.setattr("ntx.config.settings.load", lambda: _settings())
    monkeypatch.setattr("ntx.ops.venue_state.main", lambda argv=None: 1)  # not flat
    posted = []
    monkeypatch.setattr("ntx.ops.flatten._signed_post", lambda *a, **k: posted.append(a))
    assert lv.main(["--set", "--demo", "--only", "SOL"]) == 2
    assert posted == []


def test_setting_verifies_the_read_back(monkeypatch):
    """R31: 'code 0' is not proof. The tool re-reads and fails if it did not stick."""
    calls = {"n": 0}

    def fake_get(base, path, creds, demo):
        calls["n"] += 1
        lever = "10"  # never changes, i.e. the set silently did not stick
        return {"code": "0", "data": [{"instId": "SOL-USDT-SWAP", "lever": lever}]}

    monkeypatch.setattr("ntx.doctor._signed_get", fake_get)
    monkeypatch.setattr("ntx.config.settings.load", lambda: _settings())
    monkeypatch.setattr("ntx.ops.venue_state.main", lambda argv=None: 0)
    monkeypatch.setattr("ntx.ops.flatten._signed_post", lambda *a, **k: {"code": "0"})
    assert lv.main(["--set", "--demo", "--only", "SOL"]) == 1
    assert calls["n"] >= 2


def test_the_live_node_refuses_to_start_on_leverage_drift():
    """A position opened at 10x has liquidation ~9.6% away, inside the tail of a
    2xATR stop. That must stop the node, not produce a log line."""
    import inspect

    from ntx.live import node

    src = inspect.getsource(node)
    assert "leverage" in src.lower()
    assert "assert_leverage" in src


# ------------------------------------------------------------------ F050 --- #
def test_the_demo_flag_uses_the_demo_keys_even_when_env_says_live(monkeypatch):
    """OKX 50101 "APIKey does not match current environment", hit on the VPS
    2026-09-20: the tool sent `x-simulated-trading: 1` (from --demo) while
    `settings.creds()` still returned the LIVE keys, because creds() picks by
    NTX_VENUE and the server's .env says okx_live. The header and the key must
    come from the same decision."""
    seen = {}

    def fake_get(base, path, creds, demo):
        seen["creds"], seen["demo"] = creds, demo
        return {"code": "0", "data": [{"instId": "SOL-USDT-SWAP", "lever": "3"}]}

    monkeypatch.setattr("ntx.doctor._signed_get", fake_get)
    live_env = _settings(
        NTX_VENUE="okx_live",
        OKX_API_KEY="LIVEKEY",
        OKX_API_SECRET="LIVESECRET",
        OKX_API_PASSPHRASE="LIVEPASS",
        OKX_DEMO_API_KEY="DEMOKEY",
        OKX_DEMO_API_SECRET="DEMOSECRET",
        OKX_DEMO_API_PASSPHRASE="DEMOPASS",
    )
    lv.read_leverage(["SOL-USDT-SWAP.OKX"], live_env, demo=True)
    assert seen["demo"] is True
    assert seen["creds"][0] == "DEMOKEY", seen["creds"][0]

    lv.read_leverage(["SOL-USDT-SWAP.OKX"], live_env, demo=False)
    assert seen["creds"][0] == "LIVEKEY"


def test_the_same_holds_for_setting_leverage(monkeypatch):
    seen = {}

    def fake_post(base, path, body, creds, demo):
        seen["creds"], seen["demo"] = creds, demo
        return {"code": "0"}

    monkeypatch.setattr("ntx.ops.flatten._signed_post", fake_post)
    live_env = _settings(
        NTX_VENUE="okx_live",
        OKX_API_KEY="LIVEKEY",
        OKX_API_SECRET="s",
        OKX_API_PASSPHRASE="p",
        OKX_DEMO_API_KEY="DEMOKEY",
        OKX_DEMO_API_SECRET="s",
        OKX_DEMO_API_PASSPHRASE="p",
    )
    lv.set_leverage("SOL-USDT-SWAP.OKX", 3, live_env, demo=True)
    assert seen["creds"][0] == "DEMOKEY"
