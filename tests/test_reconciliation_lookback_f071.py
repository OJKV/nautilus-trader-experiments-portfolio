"""F071: a 60-minute fill lookback on a strategy that holds for days.

Every restart more than an hour after the last fill left its open positions
unmanaged. Measured on the live account, 2026-10-05, after a reboot:

    Position discrepancy for BNB-USDT-SWAP.OKX persists but no missing fills
    found; possible causes: fills outside lookback window (60min), ...
    WATCHDOG: the venue holds 3.0 BNB-USDT-SWAP that this node does not manage

BNB had been opened 2026-10-03 20:00 -- 43 hours earlier. nautilus saw the
position report, could not find the fills that built it, and gave up. The node
then had no state for a real, live, protected position: its stop still capped the
loss, but no Donchian exit and no trailing stop could run on it. On S003 that is
where the money is -- both live winners so far exited on the trailing stop.

The cause was a config default nobody had set. `reconciliation_lookback_mins`
(ours: 1440) governs start-up reconciliation. The fill query behind a *position
discrepancy* is a different knob entirely:

    position_check_lookback_mins : PositiveInt, default 60
        The lookback window (minutes) for querying fill reports when a position
        discrepancy is detected.

S003 is a 4-hour-bar strategy whose positions last days; the account was measured
flat only 31.5% of the time, with a 28.5-day worst-case holding stretch. A
60-minute window could not see a typical position's entry, so "restart" and
"stop managing whatever is open" were the same operation.

Written before the fix (R41).
"""

from __future__ import annotations

from ntx.live.node import node_config


def _exec_config():
    import inspect

    return inspect.getsource(node_config)


def test_the_fill_lookback_covers_more_than_an_hour():
    src = _exec_config()
    assert "position_check_lookback_mins" in src, (
        "the default is 60 minutes; S003 holds positions for days"
    )


def test_the_fill_lookback_covers_a_realistic_holding_period():
    """Not a token bump: it has to span how long we actually hold."""
    import re

    src = _exec_config()
    m = re.search(r"position_check_lookback_mins\s*=\s*([0-9_]+)", src)
    assert m, "set it explicitly rather than relying on the default"
    mins = int(m.group(1).replace("_", ""))
    assert mins >= 2 * 24 * 60, (
        f"{mins} min does not cover a multi-day hold; BNB was 43h old when this bit"
    )


def test_startup_reconciliation_lookback_is_still_set():
    """The two knobs are different and both matter; do not trade one for the other."""
    src = _exec_config()
    assert "reconciliation_lookback_mins" in src


def test_startup_reconciliation_sees_the_opener_of_every_close_it_sees():
    """F080: the phantom ADA short of 2026-10-06 21:14 and 22:09.

    ADA opened 10-05 04:00 and closed 10-06 16:00 (trailing stop). The startup
    reconciliation window was 1,440 min, so it saw the closing SELL (5 h old) and
    not the opening BUY (41 h old) -- and a lone sell in an empty cache is a
    short. Any window shorter than a hold manufactures a phantom on every restart
    inside it. It must span at least the same holding period as the position
    check above (R76).
    """
    import re

    src = _exec_config()
    rec = re.search(r"reconciliation_lookback_mins\s*=\s*([0-9_]+)", src)
    pos = re.search(r"position_check_lookback_mins\s*=\s*([0-9_]+)", src)
    assert rec and pos
    rec_mins = int(rec.group(1).replace("_", ""))
    pos_mins = int(pos.group(1).replace("_", ""))
    assert rec_mins >= pos_mins, (
        f"reconciliation window {rec_mins} min < position window {pos_mins} min: "
        f"a close inside it whose opener is outside becomes a phantom"
    )
