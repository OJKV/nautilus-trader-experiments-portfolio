"""The account mode decides whether ANY order can be placed (F055).

Measured on the VPS 2026-09-20, after the soak had been "running healthy" for
three hours: the OKX **demo** account was `acctLv=1` (Spot mode), where margin
swaps are not permitted. The first breakout, AVAX LONG at 16:00:01, came back
`You can't complete this request under your current account mode.` Every entry
would have done the same, for the whole seven-day soak.

`doctor` printed `acctLv` and asserted nothing (review R-07). Printing a number
nobody compares is not a check. Written before the fix (R41).
"""

from __future__ import annotations

from ntx.live.node import REQUIRED_ACCT_LV, account_mode_problem


def test_spot_mode_cannot_trade_swaps():
    bad = account_mode_problem({"acctLv": "1", "posMode": "net_mode"})
    assert bad and "acctLv" in bad and "1" in bad


def test_single_currency_margin_is_accepted():
    assert account_mode_problem({"acctLv": "2", "posMode": "net_mode"}) is None


def test_multi_currency_and_portfolio_margin_are_accepted():
    """Higher levels still permit isolated swap orders; only Spot mode does not."""
    for lv in ("3", "4"):
        assert account_mode_problem({"acctLv": lv, "posMode": "net_mode"}) is None


def test_long_short_mode_is_rejected_because_our_orders_omit_posSide():
    """Every order and the leverage call assume `net_mode` (ADR/F-notes); in
    long/short mode OKX requires `posSide` and would reject or misroute."""
    bad = account_mode_problem({"acctLv": "2", "posMode": "long_short_mode"})
    assert bad and "posMode" in bad


def test_an_unreadable_response_is_a_problem_not_a_pass():
    for body in ({}, {"acctLv": None}, {"acctLv": "abc"}):
        assert account_mode_problem(body) is not None


def test_the_required_level_is_stated_once():
    assert REQUIRED_ACCT_LV == 2


def test_the_node_checks_the_account_mode_before_it_trades():
    import inspect

    from ntx.live import node

    src = inspect.getsource(node)
    assert "assert_account_mode" in src
    main = inspect.getsource(node.main)
    assert "assert_account_mode" in main or "assert_leverage" in main


def test_doctor_fails_on_a_wrong_account_mode():
    """It printed the number and moved on, which is how this got to production."""
    import inspect

    from ntx import doctor

    src = inspect.getsource(doctor)
    assert "account_mode_problem" in src, "doctor must compare, not just print"


def test_the_node_says_which_account_it_is_on():
    """2026-09-21: the operator saw $106K in the OKX app and $0 from the bot and
    reasonably concluded something had broken. Both were right -- the bot's keys
    belong to a sub-account. Nothing anywhere said so, and the first diagnosis
    (an OKX reset) was wrong because nobody had asked *which account* first."""
    from ntx.live.node import describe_account

    sub = describe_account({"uid": "111", "mainUid": "999", "label": "nautilus-demo"})
    assert "sub-account" in sub and "nautilus-demo" in sub
    main = describe_account({"uid": "999", "mainUid": "999", "label": "main"})
    assert "main account" in main


def test_the_snapshot_and_the_monitor_carry_the_account_label():
    import inspect

    from ntx.strategies.base import NtxStrategy
    from ntx.ui import app as ui

    assert '"account_label"' in inspect.getsource(NtxStrategy.write_state)
    assert '"account_label"' in inspect.getsource(ui)
