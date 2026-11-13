"""Is it safe to restart the node right now, or would that step on a signal?

The strategy decides on a **closed 4h bar**. A restart re-requests history and
replays it without trading (F029, deliberately — history must never place
orders), so a node stopped in the minutes after a bar close evaluates that bar
only as history: the entry never happens, and nothing records it as a miss. It
looks exactly like "no signal", which is the worst kind of bug this project has.

The risk used to be theoretical. It stopped being theoretical when the deploy
timer went to one minute: a push lands, the node stops within 60 s, and the test
run holds it down for another two minutes. On 2026-09-20 the 16:00 bar arrived
at 16:00:01 and a deploy restarted the node at 16:10 — ten minutes of margin,
by luck.

So a deploy waits out the first few minutes of a bar. With a one-minute timer
the cost is a delay of at most `quiet_s`; the alternative is a silently skipped
trade.

It also waits out the last `before_s` seconds BEFORE a close (L2-09, L3-06): a
deploy that starts at 15:57 holds the node down across 16:00 just as surely as
one that starts at 16:01. `before_s` is the length of the restart it guards --
the stop, the suite, the start and the health wait.

    python -m ntx.ops.deploy_window --before 600   # exit 0 = safe, 1 = wait
"""

from __future__ import annotations

import argparse
import time

# S003 trades 4h bars, and 4h divides the day evenly, so seconds-since-midnight
# modulo the bar length is also seconds since the last close (epoch is 00:00 UTC).
BAR_S = 4 * 3600
DEFAULT_QUIET_S = 300


def seconds_since_bar(now: float | None = None, bar_s: int = BAR_S) -> int:
    return int((time.time() if now is None else now) % bar_s)


def safe_to_restart(
    now: float | None = None,
    *,
    quiet_s: int = DEFAULT_QUIET_S,
    before_s: int = 0,
    bar_s: int = BAR_S,
) -> bool:
    """False in the first `quiet_s` seconds of a bar and its last `before_s`."""
    since = seconds_since_bar(now, bar_s)
    if quiet_s > 0 and since < quiet_s:
        return False
    return not (before_s > 0 and bar_s - since <= before_s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="may a deploy restart the node now?")
    ap.add_argument("--quiet", type=int, default=DEFAULT_QUIET_S, help="seconds after a bar close")
    ap.add_argument("--before", type=int, default=0, help="seconds before a bar close")
    ap.add_argument("--now", type=float, default=None, help="epoch seconds, for testing")
    args = ap.parse_args(argv)
    since = seconds_since_bar(args.now)
    if safe_to_restart(args.now, quiet_s=args.quiet, before_s=args.before):
        return 0
    if since < args.quiet:
        wait = args.quiet - since
        why = f"{since}s since the last 4h bar close"
    else:
        wait = BAR_S - since + args.quiet
        why = f"{BAR_S - since}s until the next 4h bar close"
    print(
        f"{why}: waiting {wait}s so the restart cannot step on that bar's signal",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
