"""Two ways the demo soak was not testing what it claimed to test (2026-09-21).

At 12:00 UTC the breakout the soak had been waiting for arrived -- five coins at
once -- and every order came back `Insufficient USDT balance in account.`

1. **The demo account was empty** (trading and funding accounts both $0), and
   nothing noticed, because the soak sizes from NTX_EQUITY_OVERRIDE_USD: the
   override *claimed* $123.45 while the account held nothing. An override may
   shrink sizing to match the real account; it must never stand in for funds
   that are not there.

2. **Five orders went out, not three.** The server's .env said
   NTX_MAX_CONCURRENT_POSITIONS=5, the node let .env win over the registry, and
   every recorded gate run used 3. What runs must be what was gated (R47).

Written before the fixes (R41).
"""

from __future__ import annotations

import pytest

from ntx.config.settings import Settings


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


# ------------------------------------------------------------- funding ----- #
def test_an_empty_account_is_refused_even_with_an_override(monkeypatch):
    from ntx.live import node

    monkeypatch.setattr(node, "fetch_usdt_equity", lambda p, s: 0.0)
    p = node.plan(_settings())
    with pytest.raises(RuntimeError, match=r"(?i)fund"):
        node.assert_funded(p, _settings())


def test_an_override_larger_than_the_account_is_refused(monkeypatch):
    """Sizing as if $123.45 on an account holding $10 would size every order
    beyond what the venue will accept -- the refusals just come later."""
    from ntx.live import node

    monkeypatch.setattr(node, "fetch_usdt_equity", lambda p, s: 10.0)
    p = node.plan(_settings())
    with pytest.raises(RuntimeError, match=r"123\.45"):
        node.assert_funded(p, _settings())


def test_a_funded_demo_account_passes(monkeypatch):
    from ntx.live import node

    monkeypatch.setattr(node, "fetch_usdt_equity", lambda p, s: 5012.34)
    node.assert_funded(node.plan(_settings()), _settings())


def test_the_node_checks_funding_before_it_trades():
    import inspect

    from ntx.live import node

    main = inspect.getsource(node.main)
    assert "assert_funded" in main
    assert main.index("assert_funded") < main.index("TradingNode(")


# --------------------------------------------------------------- pinning --- #
def test_the_validated_position_cap_is_pinned_in_the_registry():
    from ntx.strategies.registry import DEPLOYED

    s003 = DEPLOYED["S003"]
    for key in ("max_concurrent_positions", "risk_equity_pct", "max_leverage"):
        assert key in s003, f"{key} is not pinned, so .env decides what runs (R47)"
    assert s003["max_concurrent_positions"] == 3, "every recorded gate run used 3"


def test_the_node_runs_the_pinned_values_not_the_env():
    from ntx.live import node

    s = _settings(NTX_MAX_CONCURRENT_POSITIONS="5")
    kw = node.strategy_kwargs(node.plan(s), s)
    assert kw["max_concurrent_positions"] == 3


def test_a_live_env_that_disagrees_with_the_gate_is_refused():
    """On live, a silent substitution is worse than a refusal: the operator
    believes one thing and the account does another."""
    from ntx.live import node

    live = _settings(
        NTX_VENUE="okx_live",
        OKX_API_KEY="k",
        OKX_API_SECRET="s",
        OKX_API_PASSPHRASE="p",
        NTX_MAX_CONCURRENT_POSITIONS="5",
    )
    live = live.model_copy(update={"equity_override_usd": None})
    with pytest.raises(RuntimeError, match="max_concurrent_positions"):
        node.assert_matches_gate(node.plan(live), live)


# ------------------------------------------------ continuous, not startup -- #
def test_an_account_emptied_while_the_node_runs_is_unhealthy():
    """OKX demo accounts reset themselves: on 2026-09-21 the demo account was
    found at acctLv=1 with a $0 balance and NO transaction history at all,
    although it had traded on 09-17/18. A start-up check does not see a reset
    that happens mid-soak, so the health check compares the two equity figures
    on every snapshot."""
    import time

    from ntx.ops.snapshot_check import problems

    snap = {
        "ts_ns": int(time.time() * 1e9),
        "is_demo": True,
        "trading_enabled": True,
        "killed": False,
        "positions_unprotected": 0,
        "positions": [],
        "instruments": [{"id": "SOL", "ready": True}],
        "sizing_equity_usd": 123.45,
        "account_equity_usd": 0.0,
    }
    found = problems(snap, now=time.time(), max_age_s=120)
    assert any("holds" in p for p in found), found


def test_the_alerter_raises_an_underfunded_account():
    import time

    from ntx.obs.alerts import alerts_from_snapshot

    snap = {
        "ts_ns": int(time.time() * 1e9),
        "positions_unprotected": 0,
        "positions": [],
        "sizing_equity_usd": 123.45,
        "account_equity_usd": 0.0,
    }
    got = alerts_from_snapshot("S003-demo", snap, now=time.time(), max_age_s=120)
    assert any(a.kind == "account_underfunded" and a.severity == "critical" for a in got)
