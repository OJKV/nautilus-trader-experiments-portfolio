"""T3-06: the snapshot wrote `equity_usd: 0.0` while venue equity was unknown.

"Unknown" and "zero" are opposite facts (B-07): the guard maps unknown to PAUSE,
but the monitor showed a $0.00 account. Every reader (`ui/app.py`,
`snapshot_check`, `alerts`) already handles None. Written before the fix (R41).
"""

from __future__ import annotations

import json
from pathlib import Path

from test_state_and_logging import _run_with_state


def test_unknown_equity_is_null_in_the_snapshot_not_zero(tmp_path):
    snap, s = _run_with_state(tmp_path)
    assert isinstance(snap["equity_usd"], float)  # a backtest account is known
    s.equity_hook = lambda: None  # the venue read went stale
    s._equity_cache = None
    s.write_state()
    snap = json.loads(Path(s.config.state_path).read_text())
    assert snap["equity_usd"] is None, "a stale read is not a $0.00 account"
    assert snap["sizing_equity_usd"] is None
