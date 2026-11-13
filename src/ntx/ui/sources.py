"""Read-only data sources for the NTX Monitor.

The monitor is a separate process that only ever *reads* files the node writes:

  data/state/<SID>-<demo|live>.json   atomic snapshot, every ~15 s
  data/journal.db, data/journal-*.db  SQLite trade journals (ntx.obs.journal)
  logs/ntx-*.json[l]                  nautilus JSON log lines
  reports/runs/*/manifest.json        backtest runs (+ metrics.json)

The one exception to "files only" is `VenueBalance`: a bot whose node has
never run has no snapshot and no journal, so the account's balance is read
straight from OKX with a signed GET (read-only, cached, time-bounded).

Nothing here writes, deletes or creates a file. SQLite is opened with
`mode=ro`, so even a bug in a query cannot modify a journal. A corrupt snapshot
reads as missing and is left in place (unlike `safe_read_json`, which deletes --
that decision belongs to the node that owns the file).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STALE_AFTER_S = 120.0

_LEVELS = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_LEVEL_ALIASES = {"DBG": "DEBUG", "INF": "INFO", "WRN": "WARNING", "WARN": "WARNING",
                  "ERR": "ERROR", "ERRO": "ERROR", "CRIT": "CRITICAL", "FATAL": "CRITICAL"}  # fmt: skip
LOG_TAIL_BYTES = 2_000_000

# The live bot is listed even when it has never run: before the node's first
# start there is no snapshot and no journal, and the owner still has to be able
# to select it to see what the live account holds. Same id journal.db maps to.
ALWAYS_LISTED = ("S003-live",)

VENUE_BALANCE_TTL_S = 30.0
VENUE_BALANCE_WAIT_S = 2.5


# ------------------------------------------------------------------ bots


def journal_bot_id(name: str) -> tuple[str, str] | None:
    """Map a journal file name to (strategy_id, env), mirroring ntx.live.node."""
    if name == "journal.db":
        return ("S003", "live")
    m = re.fullmatch(r"journal-demo-(S\d+)\.db", name)
    if m:
        return (m.group(1), "demo")
    m = re.fullmatch(r"journal-(S\d+)\.db", name)
    if m:
        return (m.group(1), "live")
    return None


@dataclass
class Bot:
    id: str
    strategy_id: str
    env: str
    snapshot_path: Path | None = None
    journal_path: Path | None = None


def discover_bots(data_dir: Path) -> dict[str, Bot]:
    bots: dict[str, Bot] = {}
    state_dir = data_dir / "state"
    if state_dir.is_dir():
        for p in sorted(state_dir.glob("*.json")):
            m = re.fullmatch(r"(S\d+)-(demo|live)", p.stem)
            if m:
                bid = p.stem
                bots[bid] = Bot(bid, m.group(1), m.group(2), snapshot_path=p)
    if data_dir.is_dir():
        for p in sorted(data_dir.glob("journal*.db")):
            ident = journal_bot_id(p.name)
            if ident is None:
                continue
            sid, env = ident
            bid = f"{sid}-{env}"
            bots.setdefault(bid, Bot(bid, sid, env)).journal_path = p
    for bid in ALWAYS_LISTED:
        sid, env = bid.rsplit("-", 1)
        bots.setdefault(bid, Bot(bid, sid, env))
    return dict(sorted(bots.items()))


# ------------------------------------------------------------------ snapshot


def read_json_obj(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def snapshot_age_s(snap: dict | None, now: datetime) -> float | None:
    if not snap:
        return None
    try:
        ts = datetime.fromisoformat(str(snap["ts"]))
    except (KeyError, ValueError):
        return None
    if ts.tzinfo is None:
        return None
    return (now - ts).total_seconds()


def is_stale(age_s: float | None) -> bool:
    return age_s is None or age_s > STALE_AFTER_S or age_s < -STALE_AFTER_S


_UNIT_S = {"SECOND": 1, "MINUTE": 60, "HOUR": 3600, "DAY": 86400}


def bar_interval_s(snap: dict) -> float | None:
    """Bar interval from an optional `bar_interval_s` or `bar_spec` snapshot field."""
    v = snap.get("bar_interval_s")
    if isinstance(v, int | float) and v > 0:
        return float(v)
    m = re.match(r"(\d+)-(SECOND|MINUTE|HOUR|DAY)\b", str(snap.get("bar_spec") or ""))
    return int(m.group(1)) * _UNIT_S[m.group(2)] if m else None


# ------------------------------------------------------------------ journal


@contextmanager
def connect_ro(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a journal read-only. The URI form handles spaces in the path."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def query(path: Path | None, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    """Run a SELECT against a journal; a missing file yields no rows (never created)."""
    if path is None or not path.is_file():
        return []
    with connect_ro(path) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


# ------------------------------------------------------------------ logs


def normalise_level(level: Any) -> str | None:
    s = str(level or "").strip().upper()
    s = _LEVEL_ALIASES.get(s, s)
    return s if s in _LEVELS else None


def level_rank(level: str | None) -> int:
    return _LEVELS.get(level or "", 0)


def _tail_lines(path: Path, max_bytes: int = LOG_TAIL_BYTES) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # first line is probably cut in half
    return lines


def read_logs(logs_dir: Path, *, min_level: str | None, n: int, bot: str | None) -> list[dict]:
    if not logs_dir.is_dir():
        return []
    prefix = f"ntx-{bot}" if bot else "ntx-"
    files = [p for p in logs_dir.glob("ntx-*.json*") if p.name.startswith(prefix) and p.is_file()]
    files.sort(key=lambda p: (p.stat().st_mtime, p.name))
    out: deque[dict] = deque(maxlen=n)
    floor = level_rank(min_level)
    for p in files:  # oldest file first, so the tail is the newest
        for line in _tail_lines(p):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            lvl = normalise_level(rec.get("level"))
            if floor and level_rank(lvl) < floor:
                continue
            out.append(
                {
                    "timestamp": rec.get("timestamp") or rec.get("ts"),
                    "level": lvl or str(rec.get("level") or ""),
                    "component": rec.get("component"),
                    "message": str(rec.get("message") if "message" in rec else line),
                    "file": p.name,
                }
            )
    return list(out)


# ------------------------------------------------------------------ backtests


def read_backtests(reports_dir: Path, limit: int = 200) -> list[dict]:
    runs_dir = reports_dir / "runs"
    if not runs_dir.is_dir():
        return []
    runs = []
    for d in runs_dir.iterdir():
        man = read_json_obj(d / "manifest.json") if d.is_dir() else None
        if man is None:
            continue
        spec = man.get("spec") if isinstance(man.get("spec"), dict) else {}
        env = man.get("environment") if isinstance(man.get("environment"), dict) else {}
        src = man.get("source_hash") if isinstance(man.get("source_hash"), dict) else {}
        runs.append(
            {
                "id": d.name,
                "label": man.get("label"),
                "created_utc": man.get("created_utc"),
                "strategy": str(spec.get("strategy_cls") or "").rsplit(".", 1)[-1] or None,
                "timeframe": spec.get("timeframe"),
                "start": spec.get("start"),
                "end": spec.get("end"),
                "git_sha": env.get("git_sha"),
                "source_hash": src.get("combined"),
                "metrics": read_json_obj(d / "metrics.json"),
            }
        )
    runs.sort(key=lambda r: (str(r["created_utc"] or ""), r["id"]), reverse=True)
    return runs[:limit]


# ------------------------------------------------------------------ venue balance


@dataclass(frozen=True)
class Balance:
    """What an account holds, and when that was actually read.

    `as_of` is the moment the venue answered, never the moment the page asked:
    a figure served from the cache must not look fresher than it is (R22).
    """

    usd: float | None
    as_of: str | None
    source: str
    error: str | None


def usdt_equity(balance: dict) -> float | None:
    """USDT equity from /api/v5/account/balance -- what the strategy sizes on (F045).

    `totalEq` would add coin dust the strategy never sees. Deliberately a copy of
    `ntx.live.node.usdt_equity` rather than an import: the monitor is a separate
    process that must not pull nautilus in. Returns None when the reply has no
    usable shape, 0.0 when it has one but holds no USDT.
    """
    try:
        details = balance["data"][0]["details"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(details, list):
        return None
    for d in details:
        if isinstance(d, dict) and d.get("ccy") == "USDT":
            try:
                return float(d.get("eq") or 0.0)
            except (TypeError, ValueError):
                return None
    return 0.0


def _live_creds(env: str) -> tuple[str, str, str, str]:
    """(key, secret, passphrase, base url) for a bot env, read from .env.

    Not `Settings.creds()`, which follows NTX_VENUE: the monitor is asked about a
    named account, and the owner switches to the live bot while the configured
    venue is the demo soak. R1: these are passed to the signer and nowhere else.
    """
    from ntx.config.settings import load

    s = load()
    if env == "demo":
        creds = (s.okx_demo_api_key, s.okx_demo_api_secret, s.okx_demo_api_passphrase)
        which = "OKX_DEMO_API_*"
    else:
        creds = (s.okx_api_key, s.okx_api_secret, s.okx_api_passphrase)
        which = "OKX_API_*"
    if not all(creds):
        raise RuntimeError(f"no {which} credentials configured")
    return (*creds, s.okx_base_url_http)


def _signed(
    method: str,
    base: str,
    path: str,
    creds: tuple[str, str, str],
    demo: bool,
    body: dict | None = None,
) -> dict:
    """Indirection over `ntx.ops.okx_rest.signed` -- imported lazily, and the seam
    the tests replace so no test can reach the venue."""
    from ntx.ops.okx_rest import signed

    return signed(method, base, path, creds, demo, body=body)


def _reason(e: BaseException) -> str:
    return (str(e) or type(e).__name__)[:160]


def fetch_venue_usdt(env: str) -> Balance:
    """One signed, read-only GET /account/balance for a bot env.

    Read-only by construction: the monitor never issues a venue write (R58 is the
    node's rule, but the same line holds here -- this process has no order path).
    """
    key, secret, passphrase, base = _live_creds(env)
    body = _signed("GET", base, "/api/v5/account/balance", (key, secret, passphrase), env == "demo")
    read_at = datetime.now(UTC).isoformat()
    if str(body.get("code")) != "0":
        why = str(body.get("msg") or body.get("_err") or body.get("code") or "no response")
        return Balance(None, None, "venue", f"balance unavailable: OKX said {why[:120]}")
    usd = usdt_equity(body)
    if usd is None:
        return Balance(None, None, "venue", "balance unavailable: no USDT row in the reply")
    return Balance(usd, read_at, "venue", None)


class VenueBalance:
    """Cached, time-bounded balance lookups for a bot with no running node.

    The page polls every 5 s and a signed call can take curl's full 20 s ceiling
    (`ntx.ops.okx_rest`), so the call runs on a worker thread: a request waits
    `wait_s` at most and otherwise reports the balance unavailable rather than
    holding the page open. Results are cached for `ttl_s` -- failures too, so an
    unreachable venue is asked once every 30 s instead of once per poll.
    """

    def __init__(
        self,
        fetch: Callable[[str], Balance] = fetch_venue_usdt,
        *,
        ttl_s: float = VENUE_BALANCE_TTL_S,
        wait_s: float = VENUE_BALANCE_WAIT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl_s = ttl_s
        self._wait_s = wait_s
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, Balance]] = {}
        self._inflight: dict[str, Future[Balance]] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ntx-ui-balance")

    def get(self, env: str) -> Balance:
        with self._lock:
            hit = self._cache.get(env)
            if hit is not None and self._clock() - hit[0] < self._ttl_s:
                return hit[1]
            fut = self._inflight.get(env)
            if fut is None or fut.done():
                fut = self._pool.submit(self._run, env)
                self._inflight[env] = fut
        try:
            return fut.result(timeout=self._wait_s)
        except FutureTimeout:
            return Balance(
                None,
                None,
                "venue",
                f"balance unavailable: the venue did not answer within {self._wait_s:g}s",
            )

    def _run(self, env: str) -> Balance:
        try:
            bal = self._fetch(env)
        except Exception as e:  # a lookup may never break the page
            bal = Balance(None, None, "venue", f"balance unavailable: {_reason(e)}")
        with self._lock:
            self._cache[env] = (self._clock(), bal)
        return bal
