"""Offline review of the trade journal.

Everything here reads `data/journal.db` and **touches no network**. That is the
point: OKX needs a VPN from Kazakhstan, so the moment the tunnel drops the venue
becomes unreachable and process memory dies with the node. The journal is local
SQLite, so every order, fill, exit and guard event stays reviewable forever
(F026).

    python -m ntx.obs.review                 # summary
    python -m ntx.obs.review --trades 20     # recent closed trades
    python -m ntx.obs.review --events        # guard / reject / kill events
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ntx.config.settings import load
from ntx.obs.journal import Journal


def summary(j: Journal) -> None:
    closed = j.closed_trades(limit=100_000)
    open_ = j.open_trades()
    print(f"journal: {j.path}")
    print(f"  open trades   : {len(open_)}")
    print(f"  closed trades : {len(closed)}")
    if not closed:
        return
    pnl = [r["pnl_net_usd"] or 0.0 for r in closed]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    gross_w, gross_l = sum(wins), -sum(losses)
    hwm = j.equity_high_water_mark()
    print(f"  net PnL       : ${sum(pnl):+,.2f}")
    print(f"  win rate      : {len(wins) / len(pnl):.1%}  ({len(wins)}W / {len(losses)}L)")
    print(f"  profit factor : {(gross_w / gross_l) if gross_l else float('inf'):.2f}")
    print(f"  equity HWM    : ${hwm:,.2f}" if hwm else "  equity HWM    : n/a")
    hrs = j.hours_since_last_closed_trade()
    print(f"  last close    : {hrs:.1f}h ago" if hrs is not None else "  last close    : never")
    by_reason: dict[str, int] = {}
    for r in closed:
        by_reason[r["exit_reason"] or "?"] = by_reason.get(r["exit_reason"] or "?", 0) + 1
    print(f"  exits         : {by_reason}")
    gaps = [r["protect_gap_ms"] for r in closed if r["protect_gap_ms"] is not None]
    if gaps:
        print(f"  protect gap   : median {sorted(gaps)[len(gaps) // 2]}ms  max {max(gaps)}ms")


def trades(j: Journal, n: int) -> None:
    print(
        f"\n{'trade':<16}{'inst':<20}{'side':<6}{'entry':>10}{'exit':>10}"
        f"{'pnl$':>9}{'reason':<16}{'bars':>5}"
    )
    for r in j.closed_trades(limit=n):
        print(
            f"{r['trade_id']:<16}{r['instrument_id']:<20}{r['side']:<6}"
            f"{r['entry_price'] or 0:>10.4f}{r['exit_price'] or 0:>10.4f}"
            f"{r['pnl_net_usd'] or 0:>9.2f}{r['exit_reason'] or '':<16}"
            f"{r['bars_held'] or 0:>5}"
        )


def events(j: Journal) -> None:
    with j._conn() as c:
        rows = c.execute(
            "SELECT ts, kind, severity, payload FROM events ORDER BY seq DESC LIMIT 40"
        ).fetchall()
    print(f"\n{'ts':<22}{'kind':<20}{'sev':<9}payload")
    for r in rows:
        print(
            f"{r['ts'][:19]:<22}{r['kind']:<20}{r['severity']:<9}"
            f"{json.dumps(json.loads(r['payload']))[:70]}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="offline journal review (no network)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--trades", type=int, default=0)
    ap.add_argument("--events", action="store_true")
    args = ap.parse_args()

    path = Path(args.db) if args.db else load().data_dir / "journal.db"
    if not path.exists():
        print(f"no journal at {path} -- nothing has traded yet")
        return 0
    j = Journal(path)
    summary(j)
    if args.trades:
        trades(j, args.trades)
    if args.events:
        events(j)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
