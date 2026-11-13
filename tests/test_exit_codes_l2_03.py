"""L2-03: "I could not tell" must not be punished like "I refuse".

Both units set `RestartPreventExitStatus=2`, deliberately: when the node refuses
to start -- not armed, wrong account mode, leverage drift, a second instance --
coming back 120 s later to refuse again is worse than staying down (R63).

But the same exit 2 was returned when a venue *read* failed. A VPS reboot, a 10 s
OKX hiccup, one call leaving over IPv6 (F061), and systemd would never retry: a
live leveraged position keeps its venue stop but loses its Donchian exits, its
trailing stop, the protection watchdog and both loss guards until a human notices.

So the two are now different exits:

    2  deterministic refusal -- the answer will be the same in 120 s. Stay down.
    4  could not determine -- ask again. systemd restarts.

Written before the fix (R41).
"""

from __future__ import annotations

import pytest

from ntx.config.geo import Verdict
from ntx.config.settings import Settings


def _settings(**over) -> Settings:
    kw = {
        "NTX_VENUE": "okx_demo",
        "OKX_DEMO_API_KEY": "k",
        "OKX_DEMO_API_SECRET": "s",
        "OKX_DEMO_API_PASSPHRASE": "p",
        "NTX_EQUITY_OVERRIDE_USD": "123.45",
        "NTX_RISK_EQUITY_PCT": "0.5",
        "NTX_MAX_LEVERAGE": "3.0",
        "NTX_MAX_CONCURRENT_POSITIONS": "3",
    }
    kw.update(over)
    return Settings(_env_file=None, **kw)


def _upto_asserts(monkeypatch, tmp_path, raiser):
    """Drive node.main() as far as the start-up assertions, then raise."""
    from ntx.live import node

    s = _settings(NTX_DATA_DIR=str(tmp_path))
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)
    monkeypatch.setattr(node, "load", lambda: s, raising=False)
    monkeypatch.setattr(node, "check_egress", lambda *a, **k: Verdict("FI", True, False, "ok"))
    monkeypatch.setattr(node, "run_preflight", lambda p, st: (p, 1000.0))
    monkeypatch.setattr(node, "assert_matches_gate", lambda p, st: None)
    monkeypatch.setattr(node, "assert_account_mode", raiser)
    monkeypatch.setattr(node, "assert_funded", lambda p, st: None)
    monkeypatch.setattr(node, "assert_leverage", lambda p, st: None)
    return node


def test_a_venue_read_that_failed_is_restartable(monkeypatch, tmp_path):
    """ "cannot read the account config: timeout" says nothing about whether we
    are allowed to trade. Ask again."""
    from ntx.live.node import Unavailable

    def raiser(p, st):
        raise Unavailable("cannot read the account config: 50011 Too Many Requests")

    node = _upto_asserts(monkeypatch, tmp_path, raiser)
    assert node.main(["--strategy", "S003"]) == 4


def test_a_venue_answer_we_do_not_like_stays_down(monkeypatch, tmp_path):
    """acctLv=1 will still be acctLv=1 in two minutes (F055)."""

    def raiser(p, st):
        raise RuntimeError("demo sub-account nautilus-demo: account is in Spot mode (acctLv=1)")

    node = _upto_asserts(monkeypatch, tmp_path, raiser)
    assert node.main(["--strategy", "S003"]) == 2


def test_unavailable_is_a_runtime_error(monkeypatch):
    """Existing `except RuntimeError` handlers -- notably observe mode -- must
    keep catching it."""
    from ntx.live.node import Unavailable

    assert issubclass(Unavailable, RuntimeError)


# ------------------------------------------------------------- egress ------ #
def test_an_egress_lookup_that_failed_is_retried_then_restartable(monkeypatch):
    """`check_egress` fails closed with country=None when the lookup itself
    failed (geo.py:183-186). That is "could not tell", not "you are in Japan"."""
    from ntx.live import node

    tries = {"n": 0}

    def flaky():
        tries["n"] += 1
        return Verdict(None, False, False, "egress check failed (TimeoutError)")

    v = node.resolve_egress(check=flaky, sleep=lambda _s: None)
    assert tries["n"] == 3, "a transient lookup deserves more than one attempt"
    assert v.country is None and not v.allowed


def test_a_recovered_egress_lookup_stops_retrying(monkeypatch):
    from ntx.live import node

    seq = [Verdict(None, False, False, "failed"), Verdict("FI", True, False, "ok")]
    v = node.resolve_egress(check=lambda: seq.pop(0), sleep=lambda _s: None)
    assert v.allowed and v.country == "FI"
    assert seq == [], "it must stop as soon as it has an answer"


def test_a_restricted_country_is_not_retried(monkeypatch):
    """A determined answer is deterministic: retrying only delays the refusal."""
    from ntx.live import node

    tries = {"n": 0}

    def restricted():
        tries["n"] += 1
        return Verdict("JP", False, False, "JP is OKX-restricted")

    v = node.resolve_egress(check=restricted, sleep=lambda _s: None)
    assert tries["n"] == 1
    assert not v.allowed and v.country == "JP"


@pytest.mark.parametrize(
    "verdict,expected",
    [
        (Verdict(None, False, False, "lookup failed"), 4),  # could not tell -> retry
        (Verdict("JP", False, False, "restricted"), 2),  # told, and refused
    ],
)
def test_main_maps_the_egress_verdict_to_the_right_exit(monkeypatch, tmp_path, verdict, expected):
    from ntx.live import node

    s = _settings(NTX_DATA_DIR=str(tmp_path))
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)
    monkeypatch.setattr(node, "load", lambda: s, raising=False)
    monkeypatch.setattr(node, "resolve_egress", lambda **k: verdict)
    assert node.main(["--strategy", "S003"]) == expected


def test_the_units_only_refuse_to_restart_on_two():
    """If 4 were ever added to RestartPreventExitStatus this whole fix is undone."""
    from pathlib import Path

    for unit in Path("deploy").glob("ntx-*.service"):
        for line in unit.read_text().splitlines():
            if line.startswith("RestartPreventExitStatus"):
                assert line.split("=", 1)[1].split() == ["2"], f"{unit.name}: {line}"
