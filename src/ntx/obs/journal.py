"""Trade journal -- durable record of what the system actually did.

Why this exists: nautilus keeps no durable trade history. Its cache database
supports Redis only and stores operational state, not analytics; its `Trader`
reports are in-memory pandas frames that vanish on restart. So every number the
web UI, `/stats`, and the live-vs-backtest comparison rely on would disappear on
the first reboot. See docs/17-persistence-and-journal.md and ADR-0012.

SQLite: one file, no server, ACID, SQL-queryable, trivially backed up. Ample at
this scale (a few thousand rows a year).

IMPORTANT: the journal is for analytics and audit. It is NEVER the source of
truth for whether a position exists -- the venue is (docs/08 §7). Position counts
used as trading gates must be verified against the venue, which is exactly the
mistake that produced 246 ghost positions in the field report (P-004).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Named table constraints, never inline keywords: failure pattern 6, where a
# find-and-replace turned `UNIQUE` into `XRPQUE` and SQLite silently accepted it,
# corrupting 47-51% of candle data for months.
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT NOT NULL,
    value TEXT NOT NULL,
    CONSTRAINT pk_meta PRIMARY KEY (key)
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT NOT NULL,
    correlation_id  TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    venue           TEXT NOT NULL,
    instrument_id   TEXT NOT NULL,
    side            TEXT NOT NULL,
    state           TEXT NOT NULL,
    opened_at       TEXT,
    closed_at       TEXT,
    qty             REAL,
    entry_price     REAL,
    exit_price      REAL,
    notional_usd    REAL,
    leverage_eff    REAL,
    risk_usd        REAL,
    stop_price      REAL,
    tsl_armed       INTEGER NOT NULL DEFAULT 0,
    exit_reason     TEXT,
    pnl_gross_usd   REAL,
    fees_usd        REAL,
    funding_usd     REAL,
    pnl_net_usd     REAL,
    slippage_bps    REAL,
    bars_held       INTEGER,
    protect_gap_ms  INTEGER,
    equity_before   REAL,
    equity_after    REAL,
    params_hash     TEXT,
    git_sha         TEXT,
    is_demo         INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT pk_trades PRIMARY KEY (trade_id)
);
CREATE INDEX IF NOT EXISTS ix_trades_closed  ON trades (closed_at);
CREATE INDEX IF NOT EXISTS ix_trades_state   ON trades (state);
CREATE INDEX IF NOT EXISTS ix_trades_inst    ON trades (instrument_id);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id      TEXT NOT NULL,
    ts              TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    instrument_id   TEXT NOT NULL,
    side            TEXT NOT NULL,
    intended_qty    REAL,
    intended_notional REAL,
    outcome         TEXT NOT NULL,
    reject_reason   TEXT,
    CONSTRAINT pk_attempts PRIMARY KEY (attempt_id)
);
CREATE INDEX IF NOT EXISTS ix_attempts_ts ON attempts (ts);

CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    trade_id    TEXT,
    payload     TEXT NOT NULL,
    CONSTRAINT pk_events PRIMARY KEY (seq AUTOINCREMENT)
);
CREATE INDEX IF NOT EXISTS ix_events_ts   ON events (ts);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events (kind);

CREATE TABLE IF NOT EXISTS equity (
    ts           TEXT NOT NULL,
    equity_usd   REAL NOT NULL,
    unrealised   REAL,
    open_positions INTEGER,
    CONSTRAINT pk_equity PRIMARY KEY (ts)
);
"""

# Every state must have a path to a terminal one, or positions accumulate
# forever with no way to close them (failure pattern 4 / P-004).
TRADE_STATES = {
    "intended": {"submitted", "rejected"},
    "submitted": {"open", "rejected", "cancelled"},
    "open": {"closing", "closed"},
    "closing": {"closed"},
    # terminal
    "closed": set(),
    "rejected": set(),
    "cancelled": set(),
}
TERMINAL_STATES = {s for s, nxt in TRADE_STATES.items() if not nxt}


class InvalidTransition(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Journal:
    """SQLite-backed trade journal.

    Path is absolute (R21) -- systemd's working directory is not the repo, which
    is how a monitoring script once reported three healthy bots as broken.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()
        if not self.path.is_absolute():  # pragma: no cover - resolve() guarantees this
            raise ValueError("journal path must be absolute")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # survives an abrupt kill
            conn.execute("PRAGMA synchronous=FULL")  # a trading record is worth an fsync
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)
            c.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    # ---------------- attempts: recorded BEFORE the venue call ---------------- #
    def record_attempt(
        self,
        *,
        attempt_id: str,
        strategy_id: str,
        instrument_id: str,
        side: str,
        intended_qty: float | None = None,
        intended_notional: float | None = None,
    ) -> None:
        """Log an entry attempt before submitting it.

        Cooldowns and rate limits key on attempts, not successes (R19). A failed
        order that leaves no record lets the next cycle retry immediately -- the
        field report saw three buys inside a 60-minute cooldown that way.
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO attempts (attempt_id, ts, strategy_id, instrument_id, side,"
                " intended_qty, intended_notional, outcome)"
                " VALUES (?,?,?,?,?,?,?, 'pending')",
                (
                    attempt_id,
                    _now(),
                    strategy_id,
                    instrument_id,
                    side,
                    intended_qty,
                    intended_notional,
                ),
            )

    def resolve_attempt(self, attempt_id: str, outcome: str, reject_reason: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE attempts SET outcome=?, reject_reason=? WHERE attempt_id=?",
                (outcome, reject_reason or None, attempt_id),
            )

    def seconds_since_last_attempt(self, instrument_id: str | None = None) -> float | None:
        q = "SELECT MAX(ts) AS ts FROM attempts"
        args: tuple = ()
        if instrument_id:
            q += " WHERE instrument_id=?"
            args = (instrument_id,)
        with self._conn() as c:
            row = c.execute(q, args).fetchone()
        if not row or not row["ts"]:
            return None
        return (datetime.now(UTC) - datetime.fromisoformat(row["ts"])).total_seconds()

    # ---------------------------- trade lifecycle ---------------------------- #
    def open_trade(self, **fields: Any) -> None:
        fields.setdefault("state", "open")
        fields.setdefault("opened_at", _now())
        cols = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        with self._conn() as c:
            c.execute(f"INSERT INTO trades ({cols}) VALUES ({marks})", tuple(fields.values()))

    def transition(self, trade_id: str, new_state: str, **updates: Any) -> None:
        """Move a trade to `new_state`, rejecting illegal transitions."""
        with self._conn() as c:
            row = c.execute("SELECT state FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown trade_id {trade_id}")
            current = row["state"]
            if new_state not in TRADE_STATES.get(current, set()):
                raise InvalidTransition(f"{current} -> {new_state} is not permitted")
            if new_state == "closed":
                updates.setdefault("closed_at", _now())
            sets = ", ".join(f"{k}=?" for k in ("state", *updates))
            c.execute(
                f"UPDATE trades SET {sets} WHERE trade_id=?",
                (new_state, *updates.values(), trade_id),
            )

    def trade_state(self, trade_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT state FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
        return row["state"] if row else None

    def update_trade(self, trade_id: str, **updates: Any) -> None:
        """Set columns on a trade without changing its state."""
        if not updates:
            return
        sets = ", ".join(f"{k}=?" for k in updates)
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE trades SET {sets} WHERE trade_id=?", (*updates.values(), trade_id)
            )
            if cur.rowcount == 0:
                raise KeyError(f"unknown trade_id {trade_id}")

    def open_trades(self) -> list[sqlite3.Row]:
        """Trades the journal believes are open.

        For analytics and reconciliation input only -- never as a trading gate
        without verifying against the venue (P-004).
        """
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM trades WHERE state NOT IN "
                "('closed','rejected','cancelled') ORDER BY opened_at"
            ).fetchall()

    def closed_trades(self, limit: int = 500) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM trades WHERE state='closed' ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def hours_since_last_closed_trade(self) -> float | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(closed_at) AS ts FROM trades WHERE state='closed'"
            ).fetchone()
        if not row or not row["ts"]:
            return None
        return (datetime.now(UTC) - datetime.fromisoformat(row["ts"])).total_seconds() / 3600

    # -------------------------------- events -------------------------------- #
    def event(
        self, kind: str, severity: str = "info", trade_id: str | None = None, **payload: Any
    ) -> None:
        import json as _json

        with self._conn() as c:
            c.execute(
                "INSERT INTO events (ts, kind, severity, trade_id, payload) VALUES (?,?,?,?,?)",
                (_now(), kind, severity, trade_id, _json.dumps(payload, default=str)),
            )

    def count_events(self, kind: str, within_seconds: float) -> int:
        cutoff = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() - within_seconds, UTC
        ).isoformat()
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM events WHERE kind=? AND ts>=?", (kind, cutoff)
            ).fetchone()
        return int(row["n"])

    # -------------------------------- equity -------------------------------- #
    def record_equity(
        self, equity_usd: float, unrealised: float = 0.0, open_positions: int = 0
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO equity (ts, equity_usd, unrealised, open_positions)"
                " VALUES (?,?,?,?) ON CONFLICT(ts) DO UPDATE SET equity_usd=excluded.equity_usd",
                (_now(), equity_usd, unrealised, open_positions),
            )

    HWM_REBASE_KEY = "hwm_rebase_ts"

    def rebase_high_water(self, equity_usd: float) -> None:
        """Forget every equity row before now, for the purpose of the high-water
        mark, and start again at `equity_usd`.

        Used when a KILL is cleared, and when money is added to or taken out of
        the account: a deposit is not a profit and a withdrawal is not a loss,
        but a drawdown guard that only knows MAX(equity) reads them as both.
        **Nothing is deleted** (R29) -- the rows stay for analysis, they simply
        stop counting towards the mark.
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self.HWM_REBASE_KEY, _now()),
            )
        self.record_equity(float(equity_usd))

    def equity_high_water_mark(self) -> float | None:
        """The highest equity since the last re-base.

        A plain MAX over the whole table cannot be corrected by writing a lower
        number afterwards -- which is why "re-basing" by appending a row quietly
        did nothing at all.
        """
        with self._conn() as c:
            since = c.execute(
                "SELECT value FROM meta WHERE key = ?", (self.HWM_REBASE_KEY,)
            ).fetchone()
            if since and since["value"]:
                row = c.execute(
                    "SELECT MAX(equity_usd) AS hwm FROM equity WHERE ts >= ?", (since["value"],)
                ).fetchone()
            else:
                row = c.execute("SELECT MAX(equity_usd) AS hwm FROM equity").fetchone()
        return row["hwm"] if row and row["hwm"] is not None else None
