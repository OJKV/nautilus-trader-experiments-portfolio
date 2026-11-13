"""Bar timing helpers for the LIVE path.

OKX stamps bars with their OPEN time, and its history endpoint returns the
still-forming bar (F034, measured with `ntx.ops.bar_probe`). Our backtest
catalog stamps bars with their CLOSE time (F014) -- each world is consistent
internally, and this helper is only used on live history.
"""

from __future__ import annotations


def history_bar_complete(ts_open_ns: int, interval_ns: int, now_ns: int) -> bool:
    """True if a bar opened at `ts_open_ns` has closed by `now_ns`."""
    return ts_open_ns + interval_ns <= now_ns
