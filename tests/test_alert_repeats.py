"""An unchanged condition is one alert, not one alert every twenty minutes.

Measured on the owner's phone, 2026-09-29 to 10-02: **1,700 criticals in 72
hours**, one every 2.5 minutes, and only four distinct facts among them --

    432  phantom_flatten_refused   SOL / LINK
    428  phantom_cache_position    SOL / LINK
    427  position_unprotected      SOL / LINK
    427  orphan_position           SOL / LINK

Two phantom positions in the live node's cache, correctly detected and correctly
refused, re-announced on every watchdog pass for three days. The guards worked.
The reporting made them unusable: the owner asked for the notifications to stop,
which is the exact failure R70 is about -- an alarm nobody can act on is an alarm
everybody learns to swipe away, and the next real one goes with it.

The fix is not to alert less. It is to alert on CHANGE: say it once, stay quiet
while nothing moves, remind at a human interval, and speak immediately when the
condition clears or a new one appears.
"""

from __future__ import annotations

from ntx.obs.alerts import Alert, suppress_repeats

HOUR = 3600.0


def _a(kind, inst="SOL-USDT-SWAP.OKX", sev="critical"):
    return Alert(kind, sev, f"{kind} on {inst}", {"instrument": inst})


def test_the_first_time_a_condition_appears_it_is_sent(tmp_path):
    out = suppress_repeats([_a("phantom_cache_position")], tmp_path, now=0.0)
    assert [x.kind for x in out] == ["phantom_cache_position"]


def test_the_same_condition_twenty_minutes_later_is_not(tmp_path):
    suppress_repeats([_a("phantom_cache_position")], tmp_path, now=0.0)
    out = suppress_repeats([_a("phantom_cache_position")], tmp_path, now=20 * 60)
    assert out == [], "this is the 1,700-alert bug"


def test_a_persistent_condition_still_reminds_once_in_a_while(tmp_path):
    """Silence forever would hide a problem that never got fixed."""
    suppress_repeats([_a("phantom_cache_position")], tmp_path, now=0.0)
    out = suppress_repeats([_a("phantom_cache_position")], tmp_path, now=6 * HOUR + 1)
    assert [x.kind for x in out] == ["phantom_cache_position"]


def test_the_reminder_says_how_long_it_has_been_going(tmp_path):
    suppress_repeats([_a("phantom_cache_position")], tmp_path, now=0.0)
    out = suppress_repeats([_a("phantom_cache_position")], tmp_path, now=6 * HOUR + 1)
    assert "6h" in out[0].text or "still" in out[0].text.lower()


def test_a_different_instrument_is_a_different_condition(tmp_path):
    suppress_repeats([_a("phantom_cache_position", inst="SOL-USDT-SWAP.OKX")], tmp_path, now=0.0)
    out = suppress_repeats(
        [_a("phantom_cache_position", inst="LINK-USDT-SWAP.OKX")], tmp_path, now=60.0
    )
    assert len(out) == 1, "LINK is news even if SOL is not"


def test_a_condition_that_clears_and_returns_alerts_again(tmp_path):
    """The whole point: a NEW occurrence must never be swallowed."""
    suppress_repeats([_a("phantom_cache_position")], tmp_path, now=0.0)
    suppress_repeats([], tmp_path, now=HOUR)  # cleared
    out = suppress_repeats([_a("phantom_cache_position")], tmp_path, now=2 * HOUR)
    assert len(out) == 1


def test_a_brand_new_kind_is_never_delayed(tmp_path):
    suppress_repeats([_a("phantom_cache_position")], tmp_path, now=0.0)
    out = suppress_repeats([_a("guard_KILL")], tmp_path, now=60.0)
    assert [x.kind for x in out] == ["guard_KILL"]


def test_suppression_survives_a_restart_of_the_alerter(tmp_path):
    """It runs from a timer: every pass is a fresh process."""
    suppress_repeats([_a("phantom_cache_position")], tmp_path, now=0.0)
    assert suppress_repeats([_a("phantom_cache_position")], tmp_path, now=60.0) == []


def test_an_unwritable_state_dir_sends_rather_than_swallows(tmp_path):
    """If dedup cannot work, err towards noise -- never towards silence."""
    missing = tmp_path / "nope" / "deeper"
    out = suppress_repeats([_a("phantom_cache_position")], missing, now=0.0)
    assert len(out) == 1


def test_the_journal_still_records_every_alert():
    """Suppression is for the phone, not for the record.

    stdout goes to the systemd journal, which is the forensic trail -- the thing
    that let the 1,700-alert flood be counted by kind in the first place. Thinning
    THAT would have hidden the bug instead of fixing it.
    """
    import inspect

    from ntx.obs import alerts as A

    src = inspect.getsource(A._main)
    printing = src.index("print(redact(a))")
    suppressing = src.index("suppress_repeats")
    assert printing < suppressing, "print every alert, then suppress only the push"
    assert "send(pushable" in src
