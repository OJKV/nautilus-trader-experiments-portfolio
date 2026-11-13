"""OKX history returns the still-forming bar (F034, measured 2026-09-17 by
`ntx.ops.bar_probe`): requested at 11:23:23, the last bar had ts_event 11:23:00
and a partial close. OKX stamps bars with their OPEN time."""

import inspect

import pytest

from ntx.strategies.bars import history_bar_complete

M = 60_000_000_000


@pytest.mark.parametrize(
    ("ts_open", "now", "ok"),
    [
        (0, M, True),  # closed exactly now
        (0, M + 1, True),
        (0, M - 1, False),  # still forming
        (M, M + 23_000_000_000, False),  # the probe case
    ],
)
def test_history_bar_complete(ts_open, now, ok):
    assert history_bar_complete(ts_open, M, now) is ok


def test_historical_path_uses_the_completeness_check():
    from ntx.strategies.base import NtxStrategy

    assert "history_bar_complete" in inspect.getsource(NtxStrategy.on_historical_data)
