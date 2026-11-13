"""NTX Monitor -- read-only FastAPI app (Phase 3.5, docs/16).

Deliberate deviation from docs/16 §3: this runs as a SEPARATE process that only
reads files, so a UI crash can never touch trading. GET endpoints only; controls
stay on the CLI (`python -m ntx.ops.flatten`).

R22: a snapshot older than 120 s is flagged `stale` and its derived health
checks report `unknown` -- stale numbers are never presented as live.
R27: the journal is analytics; a journal/snapshot position mismatch is a warning.

`/api/balance` is the one endpoint that leaves the box: a bot whose node has
never started has no snapshot and no journal, and the owner still needs to see
what the live account holds. It is a signed, read-only GET, cached and
time-bounded, and it is skipped entirely when a fresh snapshot already carries
the number.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ntx.ui import sources as src

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _ts(value: str) -> datetime:
    """Journal timestamps are ISO-8601; treat a naive one as UTC."""
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


TRADE_COLS = (
    "trade_id, instrument_id, side, state, opened_at, closed_at, qty, entry_price, "
    "exit_price, stop_price, exit_reason, pnl_gross_usd, fees_usd, funding_usd, "
    "pnl_net_usd, slippage_bps, bars_held, protect_gap_ms, is_demo"
)
OPEN_SQL = (
    f"SELECT {TRADE_COLS} FROM trades WHERE state NOT IN ('closed','rejected','cancelled') "
    "ORDER BY opened_at"
)
ALERT_SEVERITIES = ("critical", "error")
ALERT_KINDS = ("data_stall",)
_RANK = {"ok": 0, "unknown": 1, "warn": 2, "fail": 3}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def pct_of(value: float | None, basis: float | None) -> float | None:
    """`value` as a percentage of `basis`, or None when that is meaningless.

    The basis is always the SIZING BASIS -- what trades are actually sized from --
    never the account balance. On a demo bot those differ by 5x (a a sample amount account
    sizing from $1,000), so dividing by the balance would make identical strategy
    performance look five times worse on demo than on live. That is exactly
    backwards for comparing bots, which is what the percentage is for.

    Returns None rather than 0 or infinity when there is no basis: a dash is
    honest, a wrong number shown to someone being given a demo is not.
    """
    if value is None or not basis:
        return None
    return value / basis * 100.0


def create_app(
    data_dir: Path,
    logs_dir: Path,
    reports_dir: Path,
    now: Callable[[], datetime] = _utc_now,
    venue_balance: src.VenueBalance | None = None,
) -> FastAPI:
    data_dir, logs_dir, reports_dir = Path(data_dir), Path(logs_dir), Path(reports_dir)
    balances = venue_balance if venue_balance is not None else src.VenueBalance()
    app = FastAPI(title="NTX Monitor", docs_url=None, redoc_url=None, openapi_url=None)

    def _now() -> datetime:
        return now().astimezone(UTC)

    def _bot(bot: str | None) -> src.Bot:
        bots = src.discover_bots(data_dir)
        if bot is None:
            if not bots:
                raise HTTPException(404, "no bots found (no snapshots or journals)")
            # prefer a bot with a snapshot: that is the one actually running
            with_snap = [b for b in bots.values() if b.snapshot_path]
            return (with_snap or list(bots.values()))[0]
        if bot not in bots:
            raise HTTPException(404, f"unknown bot {bot!r}")
        return bots[bot]

    def _snap(b: src.Bot) -> tuple[dict | None, float | None, bool]:
        snap = src.read_json_obj(b.snapshot_path)
        age = src.snapshot_age_s(snap, _now())
        return snap, age, src.is_stale(age)

    def _badge(b: src.Bot, snap: dict | None, stale: bool) -> str:
        if snap is None:
            return "NO SNAPSHOT"
        if stale:
            return "STALE"
        return "DEMO" if snap.get("is_demo", b.env == "demo") else "LIVE"

    def _equity_block(s: dict) -> dict:
        """The two numbers that both look like equity, told apart.

        `account` is what the venue account really holds; `sizing` is what the
        strategy sizes trades from, which on the demo soak is a configured
        override (NTX_EQUITY_OVERRIDE_USD). The owner read one as the other
        once -- the same confusion that hid an empty demo account for a day
        (F059) -- so they carry their own labels and a plain-English gloss the
        page prints as a sentence rather than a tooltip.
        """
        override = s.get("equity_override_usd")
        is_override = isinstance(override, int | float)
        # `sizing_equity_usd` is the snapshot's own name for it; `equity_usd` is the
        # older alias the node still writes for readers that predate the split.
        sizing_usd = s.get("sizing_equity_usd")
        if sizing_usd is None:
            sizing_usd = s.get("equity_usd")
        if sizing_usd is None and is_override:
            sizing_usd = override
        if sizing_usd is None:
            # The live account before go-live: money in it, but no node sizing from it.
            note = (
                "No node is running for this bot, so nothing is being sized. Account "
                "balance is read straight from the venue."
            )
        elif is_override:
            note = (
                "Sizing basis is a setting (NTX_EQUITY_OVERRIDE_USD): the bot sizes every "
                "trade as if the account held that much. Account balance is the money "
                "actually in the account."
            )
        else:
            note = (
                "The bot sizes trades from the account's own balance, so both figures are "
                "the same number."
            )
        return {
            "account": {
                "label": "Account balance",
                "usd": s.get("account_equity_usd"),
                "account_label": s.get("account_label"),
            },
            "sizing": {
                "label": "Sizing basis",
                "usd": sizing_usd,
                "is_override": is_override,
            },
            "note": note,
        }

    # ------------------------------------------------------------ pages

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    class _NoCacheStatic(StaticFiles):
        """Serve the dashboard's own JS/CSS with no caching.

        The page is read from one laptop over an SSH tunnel, so caching buys
        nothing -- and it cost something real: on 2026-09-26 a shipped PnL
        percentage did not appear because the browser had re-fetched index.html
        (the label changed) while still running app.js from cache. A monitoring UI
        that silently shows old code after a deploy is worse than one that is
        slightly slower to load.
        """

        def file_response(self, *args, **kwargs):  # type: ignore[override]
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
            return resp

    app.mount("/static", _NoCacheStatic(directory=STATIC_DIR), name="static")

    # ------------------------------------------------------------ api

    @app.get("/api/bots")
    def bots(all: bool = False) -> dict:
        out = []
        for b in src.discover_bots(data_dir).values():
            # A journal with no state snapshot is a finished experiment, not a
            # bot: S900 runs for minutes, stops, and its journal stays on disk
            # forever (R29). Listing it put a permanent dead entry in the picker.
            # The history is untouched and still reachable by asking for it.
            if not all and b.snapshot_path is None and b.id not in src.ALWAYS_LISTED:
                continue
            snap, age, stale = _snap(b)
            out.append(
                {
                    "id": b.id,
                    "strategy_id": b.strategy_id,
                    "env": b.env,
                    "has_snapshot": b.snapshot_path is not None,
                    "has_journal": b.journal_path is not None,
                    "snapshot_ts": snap.get("ts") if snap else None,
                    "age_s": age,
                    "stale": stale,
                    "badge": _badge(b, snap, stale),
                }
            )
        return {"now": _now().isoformat(), "stale_after_s": src.STALE_AFTER_S, "bots": out}

    @app.get("/api/status")
    def status(bot: str | None = None) -> dict:
        b = _bot(bot)
        snap, age, stale = _snap(b)
        s = snap or {}
        return {
            "bot": b.id,
            "strategy_id": b.strategy_id,
            "env": b.env,
            "now": _now().isoformat(),
            "snapshot_ts": s.get("ts"),
            "age_s": age,
            "stale": stale,
            "badge": _badge(b, snap, stale),
            "trader_id": s.get("trader_id"),
            "venue": s.get("venue"),
            "is_demo": s.get("is_demo"),
            "trading_enabled": s.get("trading_enabled"),
            "equity_usd": s.get("equity_usd"),
            # What the account really holds vs what trades are sized from. They
            # differ on purpose on the demo soak (NTX_EQUITY_OVERRIDE_USD).
            "account_equity_usd": s.get("account_equity_usd"),
            "account_label": s.get("account_label"),
            "equity_override_usd": s.get("equity_override_usd"),
            "equity": _equity_block(s),
            "unrealized_usd": s.get("unrealized_usd"),
            # Relative to the sizing basis, so a $1,000-sized demo bot and a
            # $100 live bot can be compared at a glance.
            "unrealized_pct": pct_of(
                s.get("unrealized_usd"), s.get("sizing_equity_usd") or s.get("equity_usd")
            ),
            "positions_unprotected": s.get("positions_unprotected"),
            "open_positions": len(s.get("positions") or []) if snap else None,
            "instruments": s.get("instruments") or [],
            "counters": s.get("counters") or {},
        }

    @app.get("/api/balance")
    def balance(bot: str | None = None) -> dict:
        """What the account holds -- from the node when it is running, else the venue.

        A fresh snapshot already carries the venue's own figure, so the venue is
        only asked when there is none: the live account before go-live has no
        node, no journal and no trades, but it does have money in it. An
        unreachable venue reports why, never a blank and never a zero.
        """
        b = _bot(bot)
        snap, _age, stale = _snap(b)
        s = snap or {}
        held = s.get("account_equity_usd")
        if not stale and isinstance(held, int | float):
            bal = src.Balance(float(held), s.get("ts"), "snapshot", None)
        else:
            bal = balances.get(b.env)
        return {
            "bot": b.id,
            "env": b.env,
            "label": "Account balance",
            "ccy": "USDT",
            "usd": bal.usd,
            "source": bal.source,
            "as_of": bal.as_of,
            "account_label": s.get("account_label"),
            "error": bal.error,
        }

    @app.get("/api/positions")
    def positions(bot: str | None = None) -> dict:
        b = _bot(bot)
        snap, age, stale = _snap(b)
        return {
            "bot": b.id,
            "stale": stale,
            "age_s": age,
            "positions": (snap or {}).get("positions") or [],
            "journal_open": src.query(b.journal_path, OPEN_SQL),
        }

    @app.get("/api/trades")
    def trades(bot: str | None = None, limit: int = Query(50, ge=1, le=1000)) -> dict:
        b = _bot(bot)
        rows = src.query(
            b.journal_path,
            f"SELECT {TRADE_COLS} FROM trades WHERE state='closed' "
            "ORDER BY closed_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )
        return {"bot": b.id, "trades": rows}

    @app.get("/api/equity")
    def equity(bot: str | None = None, max_points: int = Query(1000, ge=10, le=10000)) -> dict:
        b = _bot(bot)
        rows = src.query(
            b.journal_path,
            "SELECT ts, equity_usd, unrealised, open_positions FROM equity ORDER BY ts",
        )
        if len(rows) > max_points:  # thin evenly, always keep the latest point
            step = len(rows) / max_points
            rows = [rows[int(i * step)] for i in range(max_points - 1)] + [rows[-1]]
        hwm, max_dd = None, 0.0
        for r in rows:
            eq = r["equity_usd"]
            hwm = eq if hwm is None else max(hwm, eq)
            if hwm and hwm > 0:
                r["drawdown_pct"] = (hwm - eq) / hwm * 100
                max_dd = max(max_dd, r["drawdown_pct"])
        return {"bot": b.id, "points": rows, "hwm": hwm, "max_drawdown_pct": max_dd}

    # Exits the STRATEGY chose. Everything else -- an incident, an operator, a
    # risk guard -- ended a trade for reasons that say nothing about whether the
    # edge is real. S003-demo read -1.29% while five of its twelve exits were our
    # own bugs (F065/F066/F069) and the strategy's own three were positive.
    # Deliberately a whitelist: an exit reason nobody has classified is not
    # evidence FOR the strategy.
    STRATEGY_EXITS = frozenset(
        {
            "opposite_signal",
            "stop_loss",
            "trailing_stop",
            "take_profit",
            "roi_target",
            "time_stop",
        }
    )

    @app.get("/api/stats")
    def stats(bot: str | None = None) -> dict:
        b = _bot(bot)
        closed = src.query(b.journal_path, f"SELECT {TRADE_COLS} FROM trades WHERE state='closed'")
        n_open = len(src.query(b.journal_path, OPEN_SQL))
        pnl = [r["pnl_net_usd"] or 0.0 for r in closed]
        # The denominator for every percentage below. Read from the bot's own
        # snapshot, so a bot with no node running simply has no percentage.
        snap = src.read_json_obj(b.snapshot_path) if b.snapshot_path else None
        # `equity_usd` is what older snapshots called the sizing basis; both mean
        # "what trades are sized from", which is the only honest denominator here.
        sizing = (snap or {}).get("sizing_equity_usd") or (snap or {}).get("equity_usd")
        gross_w = sum(p for p in pnl if p > 0)
        gross_l = -sum(p for p in pnl if p < 0)
        by_inst: dict[str, dict[str, Any]] = {}
        reasons: dict[str, int] = {}
        for r in closed:
            d = by_inst.setdefault(r["instrument_id"], {"count": 0, "net_usd": 0.0})
            d["count"] += 1
            d["net_usd"] = round(d["net_usd"] + (r["pnl_net_usd"] or 0.0), 10)
            key = r["exit_reason"] or "?"
            reasons[key] = reasons.get(key, 0) + 1
        gaps = [r["protect_gap_ms"] for r in closed if r["protect_gap_ms"] is not None]
        # The same picture over the trades the strategy actually finished. Totals
        # above stay whole: the money was real whoever ended the trade.
        own = [r for r in closed if (r["exit_reason"] or "") in STRATEGY_EXITS]
        own_pnl = [r["pnl_net_usd"] or 0.0 for r in own]
        own_w = sum(p for p in own_pnl if p > 0)
        own_l = -sum(p for p in own_pnl if p < 0)
        others = [r for r in closed if (r["exit_reason"] or "") not in STRATEGY_EXITS]
        # The headline is ALL TIME. The owner also asked for recent windows, by
        # close time (UTC): today, rolling 7 / 30 / 365 days, all time.
        now = datetime.now(UTC)
        starts = {
            "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
            "7d": now - timedelta(days=7),
            "30d": now - timedelta(days=30),
            "365d": now - timedelta(days=365),
            "all": None,
        }
        periods = []
        for name, since in starts.items():
            rs = [
                r for r in closed
                if since is None or (r["closed_at"] and _ts(r["closed_at"]) >= since)
            ]  # fmt: skip
            ps = [r["pnl_net_usd"] or 0.0 for r in rs]
            periods.append({
                "period": name, "closed": len(rs),
                "wins": sum(1 for x in ps if x > 0),
                "win_rate": (sum(1 for x in ps if x > 0) / len(ps)) if ps else None,
                "net_pnl_usd": round(sum(ps), 10), "net_pnl_pct": pct_of(sum(ps), sizing),
                "fees_usd": round(sum(r["fees_usd"] or 0.0 for r in rs), 10),
            })  # fmt: skip
        return {
            "periods": periods,
            "bot": b.id,
            "closed": len(closed),
            "open": n_open,
            "wins": sum(1 for p in pnl if p > 0),
            "losses": sum(1 for p in pnl if p < 0),
            "win_rate": (sum(1 for p in pnl if p > 0) / len(pnl)) if pnl else None,
            # null when undefined (no losses yet) -- JSON has no infinity
            "profit_factor": (gross_w / gross_l) if gross_l else None,
            "net_pnl_usd": sum(pnl),
            "sizing_equity_usd": sizing,
            "net_pnl_pct": pct_of(sum(pnl), sizing),
            "fees_usd": sum(r["fees_usd"] or 0.0 for r in closed),
            "funding_usd": sum(r["funding_usd"] or 0.0 for r in closed),
            "pnl_by_instrument": by_inst,
            "exit_reasons": reasons,
            "avg_protect_gap_ms": (sum(gaps) / len(gaps)) if gaps else None,
            "max_protect_gap_ms": max(gaps) if gaps else None,
            "non_strategy_exits": len(others),
            "non_strategy_exit_reasons": sorted({(r["exit_reason"] or "?") for r in others}),
            "strategy_only": {
                "closed": len(own),
                "wins": sum(1 for p in own_pnl if p > 0),
                "losses": sum(1 for p in own_pnl if p < 0),
                "win_rate": (sum(1 for p in own_pnl if p > 0) / len(own_pnl)) if own_pnl else None,
                "profit_factor": (own_w / own_l) if own_l else None,
                "net_pnl_usd": sum(own_pnl),
                "net_pnl_pct": pct_of(sum(own_pnl), sizing),
            },
        }

    @app.get("/api/events")
    def events(bot: str | None = None, limit: int = Query(50, ge=1, le=1000)) -> dict:
        b = _bot(bot)
        rows = src.query(
            b.journal_path,
            "SELECT seq, ts, kind, severity, trade_id, payload FROM events "
            "ORDER BY seq DESC LIMIT ?",
            (limit,),
        )
        for r in rows:
            with suppress(TypeError, ValueError):
                r["payload"] = json.loads(r["payload"])
        return {"bot": b.id, "events": rows}

    @app.get("/api/health")
    def health(bot: str | None = None) -> dict:
        b = _bot(bot)
        snap, age, stale = _snap(b)
        checks: list[dict] = []

        def add(name: str, status_: str, detail: str) -> None:
            checks.append({"name": name, "status": status_, "detail": detail})

        if snap is None:
            add("snapshot_fresh", "fail", "no readable snapshot -- node not running?")
        elif stale:
            add("snapshot_fresh", "fail", f"snapshot is {age:.0f}s old (> {src.STALE_AFTER_S:.0f}s)"
                if age is not None else "snapshot timestamp unreadable")  # fmt: skip
        else:
            add("snapshot_fresh", "ok", f"{age:.0f}s old")

        live = snap is not None and not stale
        why = "no fresh snapshot (R22)"
        s = snap or {}

        # positions_unprotected
        if not live:
            add("positions_unprotected", "unknown", why)
        else:
            n = s.get("positions_unprotected")
            bad = [p.get("instrument_id") for p in s.get("positions") or []
                   if p.get("protected") is False]  # fmt: skip
            if not isinstance(n, int | float):
                add("positions_unprotected", "unknown", "not reported")
            elif n > 0 or bad:
                add("positions_unprotected", "fail", f"{n} unprotected; {bad or ''}".strip())
            else:
                add("positions_unprotected", "ok", "0")

        # critical events in the last 24h (journal clock is UTC ISO)
        if b.journal_path is None or not b.journal_path.is_file():
            add("critical_events_24h", "unknown", "no journal")
        else:
            cutoff = (_now() - timedelta(hours=24)).isoformat()
            marks = ",".join("?" * len(ALERT_SEVERITIES))
            kinds = ",".join("?" * len(ALERT_KINDS))
            rows = src.query(
                b.journal_path,
                f"SELECT kind, COUNT(*) AS n FROM events WHERE ts >= ? AND ts <= ? AND "
                f"(severity IN ({marks}) OR kind IN ({kinds})) GROUP BY kind",
                (cutoff, _now().isoformat(), *ALERT_SEVERITIES, *ALERT_KINDS),
            )
            if rows:
                add("critical_events_24h", "fail",
                    ", ".join(f"{r['kind']}x{r['n']}" for r in rows))  # fmt: skip
            else:
                add("critical_events_24h", "ok", "none")

        # every instrument warm
        insts = s.get("instruments") or []
        if not live:
            add("instruments_warm", "unknown", why)
        elif not insts:
            add("instruments_warm", "warn", "no instruments reported")
        else:
            cold = [i.get("id") for i in insts if not i.get("warm")]
            add("instruments_warm", "warn" if cold else "ok",
                f"cold: {cold}" if cold else f"{len(insts)} warm")  # fmt: skip

        # last live bar age < 2x bar interval
        interval = src.bar_interval_s(s) if live else None
        if not live:
            add("bar_age", "unknown", why)
        elif interval is None:
            add("bar_age", "unknown", "bar interval not in snapshot")
        else:
            limit_s = 2 * interval
            late = [
                f"{i.get('id')} {i['live_bar_age_s']:.0f}s"
                for i in insts
                if isinstance(i.get("live_bar_age_s"), int | float)
                and i["live_bar_age_s"] > limit_s
            ]
            add("bar_age", "fail" if late else "ok",
                f"late (> {limit_s:.0f}s): {late}" if late else f"all < {limit_s:.0f}s")  # fmt: skip

        # journal vs snapshot -- warning only, the venue is the truth (R27)
        if not live:
            add("journal_vs_snapshot", "unknown", why)
        elif b.journal_path is None or not b.journal_path.is_file():
            add("journal_vs_snapshot", "unknown", "no journal")
        else:
            j_open = len(src.query(b.journal_path, OPEN_SQL))
            s_open = len(s.get("positions") or [])
            if j_open != s_open:
                add("journal_vs_snapshot", "warn",
                    f"journal open={j_open} vs node positions={s_open}; the journal is not "
                    "truth (R27) -- verify on the venue")  # fmt: skip
            else:
                add("journal_vs_snapshot", "ok", f"{s_open} open")

        overall = max((c["status"] for c in checks), key=lambda x: _RANK[x])
        return {"bot": b.id, "stale": stale, "overall": overall, "checks": checks}

    @app.get("/api/logs")
    def logs(
        level: str | None = None,
        n: int = Query(200, ge=1, le=5000),
        bot: str | None = None,
    ) -> dict:
        min_level = None
        if level:
            min_level = src.normalise_level(level)
            if min_level is None:
                raise HTTPException(422, f"unknown level {level!r}")
        return {"lines": src.read_logs(logs_dir, min_level=min_level, n=n, bot=bot)}

    @app.get("/api/backtests")
    def backtests(limit: int = Query(200, ge=1, le=2000)) -> dict:
        return {"runs": src.read_backtests(reports_dir, limit)}

    return app
