"""Clear a latched KILL, deliberately and completely.

A KILL latch (`data/state/<bot>.json.killed`) survives a restart on purpose: the
account hit its drawdown limit and a human has to look before it trades again.

Deleting the file by hand is not enough, and the old log line that suggested it
was wrong: the high-water mark in the journal is still far above the account, so
the very next risk check re-KILLs within twenty minutes. Clearing a KILL means
**both** removing the latch and re-basing the mark to where the account actually
is now.

    python -m ntx.ops.unkill --status
    python -m ntx.ops.unkill --clear --note "reviewed 2026-09-20, drawdown was real"

This is a risk decision, so it is typed, recorded in the journal, and never
automatic.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

CONFIRM = "RESUME TRADING"


def latch_paths(data_dir: Path) -> list[Path]:
    return sorted(Path(data_dir).glob("state/*.json.killed"))


def clear_kill(path: Path, *, journal=None, equity: float | None = None, note: str = "") -> None:
    """Remove the latch and re-base the high-water mark to the current equity."""
    if journal is not None and equity is not None and equity > 0:
        # A re-base, not an append: the mark is a MAX, so a lower row changes
        # nothing and the next risk check re-KILLs.
        journal.rebase_high_water(float(equity))
        with contextlib.suppress(Exception):  # the record matters more than the event
            journal.event(
                "kill_cleared",
                severity="warning",
                reason=note or "operator cleared the KILL latch",
                equity_usd=float(equity),
            )
    Path(path).unlink(missing_ok=True)


def rebase_for_transfer(journal, *, equity: float, note: str) -> None:
    """Re-base the high-water mark after money moved in or out (N2-03).

    A deposit is not a profit and a withdrawal is not a loss, but a drawdown
    guard that only knows MAX(equity) reads them as both: fund the account and
    the next real drawdown is measured from money that was never earned; take
    profit out and the guard can KILL the bot for it.
    """
    journal.rebase_high_water(float(equity))
    with contextlib.suppress(Exception):  # the re-base matters more than the event
        journal.event("hwm_rebased", severity="warning", reason=note, equity_usd=float(equity))


# Which unit runs each bot, and which key in .env reads its account. One data
# dir holds every bot's latch and journal, so the bot must be NAMED -- the old
# tool cleared every latch and picked the journal and key from .env's venue, so
# a demo recovery cleared the LIVE latch and live re-KILLed after restart (L3-01).
BOTS = {
    "S003-live": ("ntx-s003.service", None),
    "S003-demo": ("ntx-soak.service", ""),
    "S004-demo": ("ntx-s004-demo2.service", "demo2"),
}


def journal_for(data_dir: Path, bot: str) -> Path:
    from ntx.live.node import _live_journal_name

    strategy, mode = bot.split("-", 1)
    name = f"journal-demo-{strategy}.db" if mode == "demo" else _live_journal_name(strategy)
    return Path(data_dir) / name


def _env_reads(settings, bot: str) -> bool:
    """Does .env's key read THIS bot's account? Only then may equity be measured."""
    keyset = BOTS[bot][1]
    if keyset is None:
        return not settings.is_demo
    return settings.is_demo and (settings.keyset or "") == keyset


def main(argv: list[str] | None = None) -> int:
    from ntx.config.settings import load
    from ntx.obs.journal import Journal

    ap = argparse.ArgumentParser(description="clear a latched KILL")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--clear", action="store_true")
    mode.add_argument(
        "--after-transfer",
        action="store_true",
        help="money moved in or out: re-base the drawdown mark to the new equity",
    )
    ap.add_argument("--bot", choices=sorted(BOTS), help="whose latch / journal (required)")
    ap.add_argument("--note", default="", help="why it is safe to resume, for the record")
    ap.add_argument("--equity", type=float, default=None, help="re-base to this, default: measured")
    args = ap.parse_args(argv)

    settings = load()
    if not args.status and not args.bot:
        print(f"--bot is required: one of {', '.join(sorted(BOTS))}", file=sys.stderr)
        return 2

    if args.after_transfer:
        if not args.note or args.equity is None:
            print("--after-transfer needs --equity and --note", file=sys.stderr)
            return 2
        journal_path = journal_for(settings.data_dir, args.bot)
        rebase_for_transfer(Journal(journal_path), equity=args.equity, note=args.note)
        print(f"drawdown high-water mark re-based to ${args.equity:,.2f} ({args.note})")
        return 0

    latches = latch_paths(settings.data_dir)
    if args.bot:
        latches = [p for p in latches if p.name == f"{args.bot}.json.killed"]
    if not latches:
        print("no KILL latch: nothing is halted")
        return 0
    for p in latches:
        print(f"LATCHED: {p}")
        with contextlib.suppress(OSError):
            print(f"  {p.read_text().strip()}")
    if args.status:
        return 1

    if not args.note:
        print("--note is required: say why it is safe to resume", file=sys.stderr)
        return 2
    equity = args.equity
    if equity is None and not _env_reads(settings, args.bot):
        print(
            f".env's key does not read {args.bot}'s account: pass --equity <usdt> "
            f"measured on that account",
            file=sys.stderr,
        )
        return 2
    if equity is None:
        # Read the balance directly. This used to build a whole node `plan()`
        # just to get a base URL, and `plan()` enforces trading preconditions --
        # so on the box, where the demo sizing basis lives in the systemd unit
        # rather than .env, the documented recovery command died with
        # "demo session without NTX_EQUITY_OVERRIDE_USD". Clearing a flag is not
        # trading, and this is the tool the health check tells an operator to run
        # while the bot is already halted.
        from ntx.config.venues import OKX
        from ntx.live.node import usdt_equity
        from ntx.ops.okx_rest import signed

        bal = signed(
            "GET", OKX.auth_base_url, "/api/v5/account/balance", settings.creds(), settings.is_demo
        )
        if str(bal.get("code")) != "0":
            print(f"cannot read equity: {bal.get('code')} {bal.get('msg')}", file=sys.stderr)
            print("  pass --equity <usdt> to re-base without reading the venue", file=sys.stderr)
            return 2
        equity = usdt_equity(bal)
    print(f"the high-water mark will be re-based to ${equity:,.2f}")
    # Resuming after a KILL is a decision, so a human types the phrase. A drill or
    # a runbook step sets NTX_UNKILL_YES instead -- the same shape as
    # NTX_DEPLOY_LIVE_YES in deploy-live.sh.
    typed = os.environ.get("NTX_UNKILL_YES") or input(
        f"Type {CONFIRM} to clear the latch and resume: "
    )
    if typed.strip() != CONFIRM:
        print("not confirmed -- still halted")
        return 2
    for p in latches:
        journal_path = journal_for(settings.data_dir, args.bot)
        clear_kill(p, journal=Journal(journal_path), equity=equity, note=args.note)
        print(f"cleared {p}")
    unit = BOTS[args.bot][0]
    print(
        f"restart the node for it to pick this up (outside a bar's first minutes, R65): "
        f"sudo systemctl restart {unit}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
