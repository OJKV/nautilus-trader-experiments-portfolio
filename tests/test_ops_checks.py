"""The two read-only tools a deploy trusts: `venue_state` and `snapshot_check`.

Neither had a test (review Part 7), and `venue_state` printed FLAT when the API
call had failed (L-12) -- the tool the runbooks use to confirm the account is
empty was the one most likely to lie during an incident.
"""

from __future__ import annotations

import json
import time

import pytest

from ntx.ops import venue_state
from ntx.ops.snapshot_check import main as check_main
from ntx.ops.snapshot_check import problems


def _snap(**over) -> dict:
    snap = {
        "ts_ns": int(time.time() * 1e9),
        "is_demo": True,
        "trading_enabled": True,
        "positions_unprotected": 0,
        "positions": [],
        "instruments": [{"id": "SOL-USDT-SWAP.OKX", "bars_seen": 60, "warm": True, "ready": True}],
    }
    snap.update(over)
    return snap


# -------------------------------------------------------- snapshot_check --- #
def test_a_fresh_warm_protected_snapshot_is_healthy():
    assert problems(_snap(), now=time.time(), max_age_s=120) == []


def test_a_stale_snapshot_is_unhealthy():
    old = _snap(ts_ns=int((time.time() - 600) * 1e9))
    assert any("old" in p for p in problems(old, now=time.time(), max_age_s=120))


def test_an_unprotected_position_is_unhealthy():
    bad = _snap(positions_unprotected=1)
    assert any("without a venue stop" in p for p in problems(bad, now=time.time(), max_age_s=120))


def test_a_position_flagged_unprotected_is_caught_even_if_the_counter_says_zero():
    bad = _snap(positions=[{"instrument": "SOL", "protected": False}])
    assert any("no live stop" in p for p in problems(bad, now=time.time(), max_age_s=120))


def test_an_instrument_that_cannot_signal_is_unhealthy():
    """The 8.2-day warm-up dead zone (R-01) must fail a deploy, not pass it."""
    bad = _snap(instruments=[{"id": "SOL", "bars_seen": 61, "warm": True, "ready": False}])
    assert any("not ready" in p for p in problems(bad, now=time.time(), max_age_s=120))


def test_the_wrong_account_is_unhealthy():
    """A demo unit must never come up pointing at the live account."""
    live_snap = _snap(is_demo=False)
    found = problems(live_snap, now=time.time(), max_age_s=120, expect_demo=True)
    assert any("wrong account" in p for p in found)


def test_observe_mode_is_not_a_healthy_trading_node():
    bad = _snap(trading_enabled=False)
    assert any("disabled" in p for p in problems(bad, now=time.time(), max_age_s=120))


def test_cli_exit_codes(tmp_path, capsys):
    good = tmp_path / "S003-demo.json"
    good.write_text(json.dumps(_snap()))
    assert check_main([str(good), "--demo"]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_snap(positions_unprotected=2)))
    assert check_main([str(bad)]) == 1
    assert check_main([str(tmp_path / "missing.json")]) == 2


# ----------------------------------------------------------- venue_state --- #
@pytest.fixture
def no_egress_check(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        venue_state, "check_egress", lambda: SimpleNamespace(allowed=True, reason="test")
    )


def test_an_api_error_is_never_reported_as_flat(monkeypatch, no_egress_check):
    """L-12: an error body has no `data`, which used to print FLAT and exit 0."""
    monkeypatch.setattr(
        venue_state, "_signed_get", lambda *a, **k: {"code": "50011", "msg": "rate limited"}
    )
    assert venue_state.main(["--demo"]) == 2


def test_an_exception_is_never_reported_as_flat(monkeypatch, no_egress_check):
    def boom(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(venue_state, "_signed_get", boom)
    assert venue_state.main(["--demo"]) == 2


def test_a_genuinely_empty_account_is_flat(monkeypatch, no_egress_check):
    monkeypatch.setattr(venue_state, "_signed_get", lambda *a, **k: {"code": "0", "data": []})
    assert venue_state.main(["--demo", "--quiet"]) == 0


def test_an_open_position_is_not_flat(monkeypatch, no_egress_check):
    def rows(base, path, creds, demo):
        if "positions" in path:
            return {"code": "0", "data": [{"instId": "SOL-USDT-SWAP", "pos": "1"}]}
        return {"code": "0", "data": []}

    monkeypatch.setattr(venue_state, "_signed_get", rows)
    assert venue_state.main(["--demo"]) == 1


def test_a_zero_size_position_row_is_flat(monkeypatch, no_egress_check):
    def rows(base, path, creds, demo):
        if "positions" in path:
            return {"code": "0", "data": [{"instId": "SOL-USDT-SWAP", "pos": "0"}]}
        return {"code": "0", "data": []}

    monkeypatch.setattr(venue_state, "_signed_get", rows)
    assert venue_state.main(["--demo"]) == 0


def test_oco_children_and_trigger_orders_are_queried_too():
    """Attached-stop mode parks an OCO child; querying only `conditional` made it
    invisible to the tool that proves the account is clean."""
    assert {"conditional", "oco", "trigger", "move_order_stop"} <= set(venue_state.ALGO_TYPES)
    assert any("ordType=oco" in p for p in venue_state.PATHS.values())


def test_venue_state_can_be_pointed_at_either_account(monkeypatch, no_egress_check):
    """The live-deploy sequence starts with "prove the LIVE account is flat", and
    `.env` now defaults to demo -- so the tool needs an explicit --live, the same
    way `flatten` and `leverage` have one (found while rehearsing the sequence,
    2026-09-20)."""
    seen = {}

    def fake_get(base, path, creds, demo):
        seen["demo"] = demo
        seen["key"] = creds[0]
        return {"code": "0", "data": []}

    monkeypatch.setattr(venue_state, "_signed_get", fake_get)
    from ntx.config.settings import Settings

    stub = Settings(
        _env_file=None,
        NTX_VENUE="okx_demo",
        OKX_API_KEY="LIVEKEY",
        OKX_API_SECRET="s",
        OKX_API_PASSPHRASE="p",
        OKX_DEMO_API_KEY="DEMOKEY",
        OKX_DEMO_API_SECRET="s",
        OKX_DEMO_API_PASSPHRASE="p",
    )
    # `venue_state` did `from ... import load`, so the name to patch is the one
    # bound in THAT module -- patching ntx.config.settings.load would leave the
    # test reading the developer's real .env (R1).
    monkeypatch.setattr(venue_state, "load", lambda: stub)
    assert venue_state.main(["--live", "--quiet"]) == 0
    assert seen["demo"] is False and seen["key"] == "LIVEKEY"

    assert venue_state.main(["--demo", "--quiet"]) == 0
    assert seen["demo"] is True and seen["key"] == "DEMOKEY"
