"""A second demo strategy needs a second demo sub-account, and therefore a second key set.

Two nodes on one OKX account fight over the same positions (F046/R51): adoption reads venue
positions, so each would see the other's position as its own, and the orphan repair (N2-04) would
close it. That is not theory -- drills sharing the soak's account put two SOL trades into the S003
journal that the Donchian strategy never chose (F064).

So `NTX_KEYSET` selects which demo key set a node uses. It defaults to the existing one, so nothing
that runs today changes behaviour.
"""

from __future__ import annotations

import pytest

from ntx.config.settings import Settings

BASE = {
    "OKX_DEMO_API_KEY": "demo-k",
    "OKX_DEMO_API_SECRET": "demo-s",
    "OKX_DEMO_API_PASSPHRASE": "demo-p",
    "OKX_DEMO2_API_KEY": "demo2-k",
    "OKX_DEMO2_API_SECRET": "demo2-s",
    "OKX_DEMO2_API_PASSPHRASE": "demo2-p",
    "OKX_API_KEY": "live-k",
    "OKX_API_SECRET": "live-s",
    "OKX_API_PASSPHRASE": "live-p",
    "NTX_EQUITY_OVERRIDE_USD": "1000",
}


def _s(**over) -> Settings:
    kw = dict(BASE)
    kw.update(over)
    return Settings(_env_file=None, **kw)


def test_the_default_demo_keyset_is_unchanged():
    """Everything running today must keep working untouched."""
    assert _s(NTX_VENUE="okx_demo").creds() == ("demo-k", "demo-s", "demo-p")


def test_the_second_keyset_selects_the_second_demo_account():
    s = _s(NTX_VENUE="okx_demo", NTX_KEYSET="demo2")
    assert s.creds() == ("demo2-k", "demo2-s", "demo2-p")


def test_the_keyset_never_reaches_live():
    """A typo in a demo unit must not be able to point a node at real money with
    the wrong credentials, and must never silently fall back to the live key."""
    s = _s(NTX_VENUE="okx_live", NTX_KEYSET="demo2")
    with pytest.raises(ValueError, match="demo"):
        s.creds()


def test_an_unknown_keyset_is_refused_not_ignored():
    """Silently falling back to the first demo account would put two nodes on one
    sub-account, which is the exact thing this exists to prevent (R51)."""
    s = _s(NTX_VENUE="okx_demo", NTX_KEYSET="demo3")
    with pytest.raises(ValueError, match="demo3"):
        s.creds()


def test_a_selected_keyset_with_no_credentials_is_refused():
    s = Settings(
        _env_file=None,
        NTX_VENUE="okx_demo",
        NTX_KEYSET="demo2",
        OKX_DEMO_API_KEY="demo-k",
        OKX_DEMO_API_SECRET="demo-s",
        OKX_DEMO_API_PASSPHRASE="demo-p",
        NTX_EQUITY_OVERRIDE_USD="1000",
    )
    with pytest.raises(ValueError, match="OKX_DEMO2"):
        s.creds()


def test_the_keyset_is_visible_for_the_journal_and_the_snapshot():
    """Two demo bots write into one data directory; an operator must be able to
    tell which account a row came from."""
    assert _s(NTX_VENUE="okx_demo", NTX_KEYSET="demo2").keyset == "demo2"
    assert _s(NTX_VENUE="okx_demo").keyset == ""


def test_the_tool_can_check_an_alternate_keyset_before_a_node_uses_it():
    """A second key set is worthless if it turns out to address the same
    sub-account -- which is what happened on the first attempt: the new key had
    the same uid, label and balance as the old one. Check before deploying."""
    import inspect

    from ntx.ops import accounts

    assert hasattr(accounts, "for_keyset")
    assert "keyset" in inspect.signature(accounts.for_keyset).parameters
