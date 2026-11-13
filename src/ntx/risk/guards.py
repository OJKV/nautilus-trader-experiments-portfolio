"""Account-level loss guards (C-02).

`NTX_DAILY_LOSS_LIMIT_EQUITY_PCT` and `NTX_MAX_DRAWDOWN_EQUITY_PCT` existed as
settings that only `doctor` printed: nothing stopped trading when they were
breached. Pure functions here, enforced in `NtxStrategy` before every entry.

Escalation follows docs/08: a daily loss PAUSES new entries (the day resets at
UTC midnight); a drawdown from the equity high-water mark is a KILL -- flatten
and stop entering, because it means the live system is behaving unlike its
backtest, not that today was unlucky.
"""

from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    OK = "ok"
    PAUSE = "pause"  # no new entries (daily loss)
    KILL = "kill"  # flatten everything and stop entering (drawdown)


def loss_state(
    *,
    day_start_equity: float,
    high_water: float,
    equity: float,
    daily_loss_pct: float,
    max_dd_pct: float,
) -> tuple[Verdict, str]:
    """Worst applicable verdict, with the reason to log."""
    if equity <= 0:
        return Verdict.KILL, f"equity is {equity:.2f}"
    if high_water > 0 and max_dd_pct > 0:
        dd = (high_water - equity) / high_water * 100.0
        if dd >= max_dd_pct:
            return Verdict.KILL, f"drawdown {dd:.1f}% >= {max_dd_pct:.1f}% of high-water"
    if day_start_equity > 0 and daily_loss_pct > 0:
        loss = (day_start_equity - equity) / day_start_equity * 100.0
        if loss >= daily_loss_pct:
            return Verdict.PAUSE, f"daily loss {loss:.1f}% >= {daily_loss_pct:.1f}%"
    return Verdict.OK, ""


def entry_allowed(
    *,
    day_start_equity: float,
    high_water: float,
    equity: float,
    daily_loss_pct: float,
    max_dd_pct: float,
) -> tuple[bool, str]:
    """(allowed, reason). False whenever either limit is breached."""
    verdict, why = loss_state(
        day_start_equity=day_start_equity,
        high_water=high_water,
        equity=equity,
        daily_loss_pct=daily_loss_pct,
        max_dd_pct=max_dd_pct,
    )
    return verdict is Verdict.OK, why
