"""A deploy must not restart the node through a signal.

The strategy decides on a closed 4h bar. A restart re-requests history and
replays it WITHOUT trading (F029, by design) -- so if the node is stopped during
the minutes after a bar close, that bar's signal is evaluated only as history and
the trade never happens. Nothing logs it as a miss; it looks like "no signal".

Observed as a near miss on 2026-09-20: the 16:00 bar arrived at 16:00:01 and a
deploy restarted the node at 16:10. Ten minutes of margin, with a deploy timer
that fires every minute and a test suite that takes two.
"""

from __future__ import annotations

from ntx.ops.deploy_window import BAR_S, safe_to_restart, seconds_since_bar

H = 3600


def _at(hour: int, minute: int = 0, second: int = 0) -> float:
    """Epoch seconds for a UTC time of day (epoch itself is a 4h boundary)."""
    return hour * H + minute * 60 + second


def test_seconds_since_the_last_four_hour_boundary():
    assert seconds_since_bar(_at(16, 0, 0)) == 0
    assert seconds_since_bar(_at(16, 0, 30)) == 30
    assert seconds_since_bar(_at(17, 30, 0)) == 90 * 60
    assert seconds_since_bar(_at(19, 59, 59)) == BAR_S - 1


def test_a_restart_just_after_a_bar_close_is_refused():
    for when in (_at(16, 0, 1), _at(16, 2, 0), _at(16, 4, 59), _at(0, 0, 5), _at(20, 1, 0)):
        assert safe_to_restart(when, quiet_s=300) is False, when


def test_a_restart_well_inside_a_bar_is_allowed():
    for when in (_at(16, 6, 0), _at(17, 30, 0), _at(19, 59, 0), _at(2, 0, 0)):
        assert safe_to_restart(when, quiet_s=300) is True, when


def test_the_window_is_configurable_and_can_be_disabled():
    assert safe_to_restart(_at(16, 2, 0), quiet_s=0) is True
    assert safe_to_restart(_at(16, 20, 0), quiet_s=1800) is False


def test_cli_exit_codes():
    from ntx.ops.deploy_window import main

    assert main(["--now", str(int(_at(17, 30)))]) == 0
    assert main(["--now", str(int(_at(16, 1)))]) == 1


def test_the_deploy_script_consults_it_before_stopping_the_node():
    from pathlib import Path

    update = (Path(__file__).resolve().parents[1] / "deploy" / "update.sh").read_text()
    assert "deploy_window" in update
    # Anchor on the DEPLOY's stop, not the rollback's (which also stops units).
    assert update.index("deploy_window") < update.index('log "stopping $u"')


# ---- L2-09 / L3-06: the other side of the close ---------------------------- #
# A deploy that starts at 15:57 holds the node down across the 16:00 close just
# as surely as one that starts at 16:01. Only the after-side was guarded.


def test_a_restart_just_before_a_bar_close_is_refused():
    for when in (_at(15, 51, 0), _at(15, 57, 0), _at(15, 59, 59), _at(23, 55, 0)):
        assert safe_to_restart(when, quiet_s=300, before_s=600) is False, when


def test_the_before_side_leaves_the_middle_of_the_bar_open():
    for when in (_at(15, 49, 0), _at(17, 30, 0), _at(16, 6, 0)):
        assert safe_to_restart(when, quiet_s=300, before_s=600) is True, when


def test_cli_refuses_before_the_close():
    from ntx.ops.deploy_window import main

    assert main(["--now", str(int(_at(15, 57))), "--before", "600"]) == 1
    assert main(["--now", str(int(_at(15, 40))), "--before", "600"]) == 0
