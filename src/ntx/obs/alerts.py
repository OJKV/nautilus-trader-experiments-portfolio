"""Alerting and the dead-man switch (docs/23-live-observability.md §4).

Two jobs that must not be the same process:

* **Alerter** — reads local files only (journal + state snapshot), so it still
  works when the venue is unreachable, and pushes what went wrong.
* **Dead-man switch** — pings an outside service on every healthy run. If the
  box, the timer or the network dies, the ping stops and the outside service
  pages you. An alarm inside the bot cannot report the bot's own death.

Run from a systemd timer every 5 minutes (docs/21 Stage 11):

    python -m ntx.obs.alerts            # one pass
    python -m ntx.obs.alerts --message "ntx-s003 crashed"   # OnFailure= hook

Configuration (`.env`, all optional -- with none set this is a no-op):
    NTX_ALERT_NTFY_URL   e.g. https://ntfy.sh/ntx-<random-string>
    NTX_HEALTHCHECK_URL  e.g. https://hc-ping.com/<uuid>

Privacy (R1, docs/23 §7): a push topic is a semi-public channel. Alerts carry
the decision -- kind, severity, instrument, reason, percentages -- and never
equity, PnL, sizes, ids or anything key-shaped.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sqlite3
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ntx.config.venues import BROWSER_UA
from ntx.obs.atomic import atomic_write_json, safe_read_json

CURSOR_NAME = "alert-cursor.json"
ALERT_SEVERITIES = ("warning", "critical")
# Past this age an unexpected snapshot is treated as a finished run, not an outage.
RETIRED_AFTER_S = 24 * 3600
# A deploy stops the node for a couple of minutes to swap the code, which is
# longer than the staleness limit. `deploy/update.sh` writes this marker for the
# duration so a routine deploy does not page anyone (F053). Only staleness is
# excused, never an unprotected position; and a marker older than
# MAINTENANCE_MAX_S is ignored, so a deploy that died cannot silence the alerter.
MAINTENANCE_NAME = ".deploy-in-progress"
MAINTENANCE_MAX_S = 15 * 60
# Values that must never leave the box; everything else in `extra` may travel.
_FORBIDDEN_KEYS = (
    "equity",
    "pnl",
    "qty",
    "size",
    "sz",
    "notional",
    "balance",
    "key",
    "secret",
    "passphrase",
    "token",
    "price",
)


@dataclass
class Alert:
    kind: str
    severity: str
    text: str
    extra: dict = field(default_factory=dict)
    seq: int = 0
    source: str = ""


# ------------------------------------------------------------------ cursor -- #
def load_cursor(data_dir: Path, bot: str) -> int:
    data = safe_read_json(Path(data_dir) / CURSOR_NAME) or {}
    return int(data.get(bot, 0))


def save_cursor(data_dir: Path, bot: str, seq: int) -> None:
    path = Path(data_dir) / CURSOR_NAME
    data = safe_read_json(path) or {}
    data[bot] = int(seq)
    atomic_write_json(path, data)


TRADE_CURSOR_NAME = "alert-trade-cursor.json"


def load_trade_cursor(data_dir: Path, bot: str) -> dict:
    """How far this bot's *trades* have been announced.

    Two marks, not one. A trade row is written when it opens and updated when it
    closes, so a single high-water mark over `opened_at` would swallow the close
    of a trade whose open was already sent.
    """
    data = safe_read_json(Path(data_dir) / TRADE_CURSOR_NAME) or {}
    got = data.get(bot) or {}
    return {
        "opened_at": str(got.get("opened_at") or ""),
        "closed_at": str(got.get("closed_at") or ""),
    }


def save_trade_cursor(data_dir: Path, bot: str, cursor: dict) -> None:
    path = Path(data_dir) / TRADE_CURSOR_NAME
    data = safe_read_json(path) or {}
    prev = data.get(bot) or {}
    data[bot] = {
        # Never move a mark backwards: a partial delivery leaves the rest to be
        # re-read next tick, and re-reading is free where losing one is not.
        k: max(str(cursor.get(k) or ""), str(prev.get(k) or ""))
        for k in ("opened_at", "closed_at")
    }
    atomic_write_json(path, data)


def trade_cursor_from(alerts: list[Alert]) -> dict:
    """The marks implied by these trade alerts."""
    cursor = {"opened_at": "", "closed_at": ""}
    for a in alerts:
        key = "closed_at" if a.kind == "trade_closed" else "opened_at"
        at = str((a.extra or {}).get("at") or "")
        if at > cursor[key]:
            cursor[key] = at
    return cursor


SEEN_NAME = "alert-seen.json"
# How long a condition may persist before it is worth saying again. Short enough
# that a real problem is not forgotten overnight, long enough that it is never
# the reason somebody mutes the channel.
REPEAT_AFTER_S = 6 * 3600


def _fingerprint(alert: Alert) -> str:
    """What makes two alerts the same fact: the kind, whose it is, what it is about.

    Whose matters (O3-02): the demo bots trade the live universe, and a demo
    `position_unprotected SOL` used to mute the live one for six hours. How many
    matters too: one naked position becoming two is news, not a repeat.
    Journal events are prefixed `j:` -- they expire by time, not by absence (O3-03).
    """
    extra = alert.extra or {}
    parts = (
        alert.kind,
        alert.source,
        str(extra.get("bot") or ""),
        str(extra.get("instrument") or ""),
        str(extra.get("count") if extra.get("count") is not None else ""),
    )
    return ("j:" if alert.seq else "") + "|".join(parts)


def suppress_repeats(alerts: list[Alert], data_dir, now: float | None = None) -> list[Alert]:
    """Drop alerts whose condition we have already reported and that has not moved.

    Measured 2026-09-29..10-02: 1,700 criticals in 72 hours carrying four facts.
    Two phantom positions, correctly detected and correctly refused, re-announced
    on every watchdog pass for three days until the owner asked for the
    notifications to stop. That is the failure R70 describes -- an alarm nobody
    can act on trains everybody to swipe it away, and the next real one goes with
    it.

    So alert on CHANGE. A fact that is new, or that cleared and came back, goes
    out at once. A fact that has not moved goes out again after `REPEAT_AFTER_S`,
    saying how long it has been true. A fact that has stopped is forgotten, so its
    return is news again.

    Never swallows on error: if the state cannot be read or written, everything is
    sent. Too many alerts is a bad day; a missing one is a bad month.
    """
    now = time.time() if now is None else now
    path = Path(data_dir) / SEEN_NAME
    try:
        seen = safe_read_json(path) or {}
    except Exception:
        return alerts
    if not isinstance(seen, dict):
        seen = {}

    out: list[Alert] = []
    fresh: dict[str, float] = {}
    # A journal event exists in exactly one pass -- its seq is new once -- so
    # "absent from this pass" says nothing about whether it has cleared. Those
    # fingerprints are kept until the repeat window runs out instead (O3-03).
    for fp, first in seen.items():
        if fp.startswith("j:"):
            with contextlib.suppress(TypeError, ValueError):
                if now - float(first) < REPEAT_AFTER_S:
                    fresh[fp] = float(first)
    for alert in alerts:
        fp = _fingerprint(alert)
        if fp.startswith("j:") and fp in fresh and fp not in seen:
            continue  # the same journal fact twice in one pass: one push
        first = seen.get(fp)
        if first is None:
            fresh[fp] = now
            out.append(alert)
            continue
        try:
            first = float(first)
        except (TypeError, ValueError):
            first = now
        age = now - first
        if age >= REPEAT_AFTER_S:
            fresh[fp] = now  # restart the clock; the next reminder is another window away
            hours = int(age // 3600)
            out.append(
                Alert(
                    alert.kind,
                    alert.severity,
                    f"{alert.text} (still true after {hours}h)",
                    alert.extra,
                    alert.seq,
                    alert.source,
                )
            )
        else:
            # Unchanged and recent: keep the ORIGINAL first-seen time, or a
            # condition that never clears would reset its own clock every pass
            # and never remind at all.
            fresh[fp] = first
    # The alerts have already been chosen; failing to remember only costs noise.
    with contextlib.suppress(Exception):
        atomic_write_json(path, fresh)
    return out


ONESHOT_SEEN_NAME = "alert-seen-oneshot.json"


def _read_dedup_state(data_dir) -> dict:
    """The dedup files' bytes as they are now, so an undelivered pass can be undone."""
    out = {}
    for name in (SEEN_NAME, ONESHOT_SEEN_NAME):
        try:
            out[name] = (Path(data_dir) / name).read_bytes()
        except OSError:
            out[name] = None
    return out


def _restore_dedup_state(data_dir, before: dict) -> None:
    """Undo what an undelivered pass remembered. Re-sending is noise; forgetting is loss."""
    for name, raw in before.items():
        path = Path(data_dir) / name
        with contextlib.suppress(OSError):
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(raw)


def suppress_repeated_message(alert: Alert, data_dir, now: float | None = None) -> bool:
    """Should this one-shot message be pushed? True the first time, then once per window.

    A deploy that loops every 65 s re-sends the same notice every 65 s; that is
    the 1,700-alert flood (F070) arriving through a side door.

    It cannot share `suppress_repeats`' state. That function treats each call as
    the COMPLETE set of current conditions and forgets anything absent from it,
    so a single message routed through it would wipe the main alerter's memory and
    re-page every standing condition on its next pass. One-shot messages have no
    "cleared" signal, so they expire on time instead.

    Never swallows on error: if the state cannot be read or written, it sends.
    """
    now = time.time() if now is None else now
    path = Path(data_dir) / ONESHOT_SEEN_NAME
    fp = f"{alert.kind}|{alert.text}"
    try:
        seen = safe_read_json(path) or {}
        if not isinstance(seen, dict):
            seen = {}
        last = seen.get(fp)
        if last is not None and now - float(last) < REPEAT_AFTER_S:
            return False
        # Forget anything old enough that it would be sent again anyway, so the
        # file cannot grow without bound across months of deploys.
        seen = {k: v for k, v in seen.items() if now - float(v) < REPEAT_AFTER_S}
        seen[fp] = now
        atomic_write_json(path, seen)
    except Exception:
        return True
    return True


# ------------------------------------------------------------------ inputs -- #
def collect(journal, since_seq: int = 0) -> list[Alert]:
    """Journal events newer than `since_seq`, warning and critical only."""
    path = journal.path if hasattr(journal, "path") else Path(journal)
    out: list[Alert] = []
    uri = f"file:{Path(path)}?mode=ro"

    def _unreadable(e: Exception) -> list[Alert]:
        # Silence here used to read as "no alerts" and then pinged healthy.
        return [
            Alert(
                "journal_unreadable",
                "critical",
                f"{Path(path).stem}: cannot read the journal ({e})",
                {"journal": Path(path).name},
            )
        ]

    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as e:
        return _unreadable(e)
    try:
        con.row_factory = sqlite3.Row
        top = con.execute("SELECT MAX(seq) FROM events").fetchone()[0]
        if top is not None and int(top) < since_seq:
            # The journal is younger than the cursor: recreated or restored, its
            # seq restarted at 1, and every new event sat below the cursor and was
            # never read (O2-02). Read it from the start, and say so.
            out.append(
                Alert(
                    "journal_reset",
                    "warning",
                    f"{Path(path).stem}: journal restarted below the alert cursor"
                    f" ({top} < {since_seq}) -- re-reading it from the start",
                    {"journal": Path(path).name},
                )
            )
            since_seq = 0
        rows = con.execute(
            "SELECT seq, kind, severity, payload FROM events "
            "WHERE seq > ? AND severity IN (?, ?) ORDER BY seq",
            (since_seq, *ALERT_SEVERITIES),
        ).fetchall()
    except sqlite3.Error as e:
        return _unreadable(e)
    finally:
        con.close()
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except Exception:
            payload = {}
        out.append(
            Alert(
                kind=r["kind"],
                severity=r["severity"],
                text=str(payload.get("reason") or payload.get("detail") or r["kind"]),
                extra=payload,
                seq=int(r["seq"]),
                source=Path(path).stem,
            )
        )
    return out


def trade_alerts(journal_path, *, since: dict | None = None) -> list[Alert]:
    """A line for every trade that opened or closed since the cursor.

    These are the only alerts that fire when nothing is wrong, so they carry
    `info` severity and must never buzz like an unprotected position does.

    Privacy (R1, docs/23 §7): bot, side, instrument, reason and **percentages**.
    A percentage says how the trade went; it says nothing about how much money
    is behind it, which is exactly the line a semi-public push topic has to
    respect. Dollars, sizes and trade ids stay in the dashboard.
    """
    path = Path(journal_path)
    since = since or {"opened_at": "", "closed_at": ""}
    bot = _journal_bot(path)
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        # An unreadable journal is already reported by `collect` as critical;
        # saying it twice would only make the phone noisier.
        return []
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM trades WHERE (opened_at IS NOT NULL AND opened_at > ?) "
            "OR (closed_at IS NOT NULL AND closed_at > ?) ORDER BY opened_at, closed_at",
            (since.get("opened_at") or "", since.get("closed_at") or ""),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()

    out: list[Alert] = []
    for r in rows:
        inst = str(r["instrument_id"]).removesuffix(".OKX")
        side = str(r["side"])
        sign = -1.0 if side.upper() == "SHORT" else 1.0
        opened, closed = r["opened_at"], r["closed_at"]
        if closed and closed > (since.get("closed_at") or ""):
            move = ""
            entry, exit_px = r["entry_price"], r["exit_price"]
            if entry and exit_px:
                move = f" {(exit_px - entry) / entry * 100.0 * sign:+.2f}%"
            why = r["exit_reason"] or "closed"
            out.append(
                Alert(
                    "trade_closed",
                    "info",
                    f"{bot} closed {side} {inst}{move} ({why})",
                    {"bot": bot, "instrument": inst, "at": str(closed)},
                )
            )
        elif opened and opened > (since.get("opened_at") or "") and r["state"] != "submitted":
            stop = ""
            entry, stop_px = r["entry_price"], r["stop_price"]
            if entry and stop_px:
                stop = f", stop {abs(entry - stop_px) / entry * 100.0:.1f}% away"
            out.append(
                Alert(
                    "trade_opened",
                    "info",
                    f"{bot} opened {side} {inst}{stop}",
                    {"bot": bot, "instrument": inst, "at": str(opened)},
                )
            )
    return out


def _bot_id(path: Path) -> str:
    stem = path.stem  # S003-demo
    return stem


def maintenance_scope(data_dir: Path, *, now: float | None = None) -> str | None:
    """Is a deploy swapping the code right now, and for which role?

    The marker is `<unix-ts> [demo|live]`. None: no deploy in progress. "": a
    deploy that did not say whose it is, which excuses every bot (the original
    format). "demo" / "live": only that role's bots -- one shared file used to let
    a demo deploy excuse a LIVE node going quiet for fifteen minutes (O3-07).
    """
    path = Path(data_dir) / MAINTENANCE_NAME
    try:
        parts = path.read_text().split()
        started = float(parts[0])
    except (OSError, ValueError, IndexError):
        return None
    age = (time.time() if now is None else now) - started
    if not 0 <= age <= MAINTENANCE_MAX_S:
        return None
    return parts[1] if len(parts) > 1 else ""


def maintenance_active(data_dir: Path, *, now: float | None = None) -> bool:
    """Is a deploy swapping the code right now?"""
    return maintenance_scope(data_dir, now=now) is not None


def _bot_role(bot: str, snap: dict | None = None) -> str:
    """'demo' or 'live'. Unknown reads as live: a demo deploy must not excuse it."""
    if isinstance(snap, dict) and "is_demo" in snap:
        return "demo" if snap.get("is_demo") else "live"
    return "demo" if bot.endswith("-demo") else "live"


def _excused(scope: str | None, bot: str, snap: dict | None = None) -> bool:
    """Does the deploy in progress excuse this bot's staleness?"""
    return scope is not None and scope in ("", _bot_role(bot, snap))


def alerts_from_snapshot(
    bot: str,
    snap: dict,
    *,
    now: float,
    max_age_s: float,
    expected: bool = False,
    maintenance: bool = False,
    kill_latched: bool = False,
) -> list[Alert]:
    """Everything one state snapshot can tell us.

    Two rules the first version got wrong (review 2026-09-18):
    * age is `abs()` -- a future-dated snapshot (clock skew) was never stale;
    * `positions[].protected` is authoritative, not the bot's own
      `positions_unprotected` counter, which cannot see a stop that died at the
      venue or a position the strategy has no state for (O-03/B-03).
    """
    out: list[Alert] = []
    age = abs(now - float(snap.get("ts_ns", 0)) / 1e9)
    retired = not expected and age > RETIRED_AFTER_S
    if retired:
        return out
    if snap.get("killed") or kill_latched:
        # A latched KILL blocks every entry until a human clears it, so it is
        # said again every REPEAT_AFTER_S for as long as it holds. It used to be
        # in the snapshot and read by nobody: one lost push and it was silent (O3-06).
        out.append(
            Alert(
                "bot_killed",
                "critical",
                f"{bot}: KILL is latched -- no new entries until cleared (ops.unkill)",
                {"bot": bot},
            )
        )
    if age > max_age_s and not maintenance:
        out.append(
            Alert(
                "snapshot_stale",
                "critical",
                f"{bot}: no update for {age / 60:.0f} min -- node down or frozen?",
                {"bot": bot, "age_min": round(age / 60, 1)},
            )
        )
    held, sized = snap.get("account_equity_usd"), snap.get("sizing_equity_usd")
    if held is not None and sized is not None and float(sized) > float(held):
        out.append(
            Alert(
                "account_underfunded",
                "critical",
                # A ratio, not dollars: amounts never go to the push topic (O3-09).
                f"{bot}: account holds {float(held) / float(sized) * 100.0:.0f}% of the"
                f" equity it sizes for -- every order will be refused",
                {"bot": bot},
            )
        )
    positions = snap.get("positions") or []
    naked = [p for p in positions if p.get("protected") is False]
    unprotected = max(int(snap.get("positions_unprotected") or 0), len(naked))
    if unprotected:
        out.append(
            Alert(
                "positions_unprotected",
                "critical",
                f"{bot}: {unprotected} position(s) without a venue stop (R15)",
                {"bot": bot, "count": unprotected},
            )
        )
    return out


def build_alerts(
    data_dir: Path,
    *,
    max_age_s: float = 120.0,
    journals: list[Path] | None = None,
    expect: list[str] | None = None,
    now: float | None = None,
    fills: bool = True,
) -> list[Alert]:
    """Everything worth waking a human for, from local files only."""
    data_dir = Path(data_dir)
    now = time.time() if now is None else now
    scope = maintenance_scope(data_dir, now=now)
    alerts: list[Alert] = []
    seen: set[str] = set()

    for snap_path in sorted((data_dir / "state").glob("*.json")):
        bot = _bot_id(snap_path)
        seen.add(bot)
        latched = snap_path.with_name(snap_path.name + ".killed").exists()
        # Never delete: it is the bot's file, not ours, and a half-written one is
        # evidence (O3-10).
        snap = safe_read_json(snap_path, delete_corrupt=False)
        # One bad file must cost one bot's alerts, not everybody's: a null ts_ns
        # or a string count used to raise out of here and take the whole pass --
        # and every other bot's incident -- down with it (O3-04).
        try:
            if not isinstance(snap, dict) or not snap:
                raise ValueError("not a JSON object")
            # A snapshot nobody expects and that stopped updating long ago belongs
            # to a finished rehearsal, not to an incident. Declare the bots that
            # must be running in NTX_ALERT_BOTS and they alarm at any age.
            alerts.extend(
                alerts_from_snapshot(
                    bot,
                    snap,
                    now=now,
                    max_age_s=max_age_s,
                    expected=bot in (expect or []),
                    maintenance=_excused(scope, bot, snap),
                    kill_latched=latched,
                )
            )
        except Exception as e:
            alerts.append(
                Alert(
                    "snapshot_unreadable",
                    "critical",
                    f"{bot}: unreadable ({type(e).__name__})",
                    {"bot": bot},
                )
            )
            if latched:
                alerts.append(
                    Alert("bot_killed", "critical", f"{bot}: KILL is latched", {"bot": bot})
                )

    for bot in expect or []:
        if bot not in seen and not _excused(scope, bot):
            alerts.append(
                Alert(
                    "snapshot_missing",
                    "critical",
                    f"{bot}: no state snapshot at all",
                    {"bot": bot},
                )
            )

    for jpath in journals or []:
        jpath = Path(jpath)
        since = load_cursor(data_dir, _journal_bot(jpath))
        alerts.extend(collect(jpath, since_seq=since))
        if fills:
            alerts.extend(
                trade_alerts(jpath, since=load_trade_cursor(data_dir, _journal_bot(jpath)))
            )
    # The cursor moves in `advance_cursors`, AFTER delivery: a dry run (or a
    # crash between reading and sending) must not swallow an alert.
    return alerts


BACKUP_STALE_AFTER_S = 36 * 3600  # nightly timer + a missed night's margin


def backup_alerts(data_dir: Path, journals: list[Path], *, now: float | None = None) -> list[Alert]:
    """A journal whose newest backup is old, or that has none (O3-08).

    The backup's own failure pushes through OnFailure=; this catches the cases
    that never fail -- a timer that stopped, a unit never installed.
    """
    now = time.time() if now is None else now
    out: list[Alert] = []
    for jpath in journals:
        name = Path(jpath).stem
        copies = list((Path(data_dir) / "backups").glob(f"{name}-[0-9]*Z.db"))
        newest = max((c.stat().st_mtime for c in copies), default=None)
        if newest is None or now - newest > BACKUP_STALE_AFTER_S:
            age = "never" if newest is None else f"{(now - newest) / 3600:.0f}h ago"
            out.append(
                Alert(
                    "backup_stale",
                    "warning",
                    f"{name}: last journal backup {age} (scripts/journal_backup.sh)",
                    {"journal": Path(jpath).name},
                )
            )
    return out


def _journal_bot(path: Path) -> str:
    stem = Path(path).stem
    return "S003-live" if stem == "journal" else stem.replace("journal-", "")


def fresh_bots(
    data_dir: Path, expect: list[str], *, max_age_s: float = 120.0, now: float | None = None
) -> list[str]:
    """Which declared bots wrote a recent snapshot. Health means all of them.

    A bot the deploy in progress excuses counts as fresh: the alerts already
    forgave its staleness, and the dead-man switch pinging `/fail` for the same
    routine restart paged the operator anyway (O2-04).
    """
    out = []
    now = time.time() if now is None else now
    scope = maintenance_scope(data_dir, now=now)
    for bot in expect:
        snap = safe_read_json(Path(data_dir) / "state" / f"{bot}.json", delete_corrupt=False)
        if not isinstance(snap, dict):
            snap = None
        if _excused(scope, bot, snap):
            out.append(bot)
            continue
        try:
            if snap and abs(now - float(snap["ts_ns"]) / 1e9) <= max_age_s:
                out.append(bot)
        except (KeyError, TypeError, ValueError):
            pass  # unreadable is not fresh; build_alerts says why (O3-04)
    return out


def advance_cursors(data_dir: Path, alerts: list[Alert]) -> None:
    """Mark journal alerts as delivered. Call only after `send` returns."""
    highest: dict[str, int] = {}
    for a in alerts:
        if not a.seq or not a.source:
            continue
        bot = _journal_bot(Path(a.source))
        highest[bot] = max(highest.get(bot, 0), a.seq)
    for bot, seq in highest.items():
        save_cursor(Path(data_dir), bot, seq)
    # Trade alerts carry their own marks: they come from the trades table, not
    # from the event sequence, and a row is read twice in its life.
    by_bot: dict[str, list[Alert]] = {}
    for a in alerts:
        if a.kind in ("trade_opened", "trade_closed"):
            by_bot.setdefault(str((a.extra or {}).get("bot") or ""), []).append(a)
    for bot, got in by_bot.items():
        if bot:
            save_trade_cursor(Path(data_dir), bot, trade_cursor_from(got))


# ---------------------------------------------------------------- delivery -- #
# Amounts in free TEXT -- `$37.32`, `equity=1000.00`, `stored=5101.70`. `extra` is
# filtered by key, but a journal reason or a snapshot line is text (O3-09).
_DOLLARS = re.compile(r"\$\s?-?[\d,]+(?:\.\d+)?")
_AMOUNT_KV = re.compile(
    r"\b(\w*(?:" + "|".join((*_FORBIDDEN_KEYS, "stored", "hwm")) + r")\w*)=-?[\d,.]+",
    re.IGNORECASE,
)


def _scrub(text: str) -> str:
    return _AMOUNT_KV.sub(r"\1=…", _DOLLARS.sub("$…", text))


def redact(alert: Alert) -> str:
    """One line, safe to send to a third-party push topic."""
    keep = {
        k: v
        for k, v in (alert.extra or {}).items()
        if not any(bad in k.lower() for bad in _FORBIDDEN_KEYS)
    }
    keep.pop("reason", None)
    keep.pop("detail", None)
    bits = " ".join(f"{k}={v}" for k, v in sorted(keep.items()))
    text = _scrub(alert.text)
    return f"[{alert.severity.upper()}] {alert.kind}: {text}" + (f" ({bits})" if bits else "")


def _post(url: str, data: bytes | None, headers: dict | None = None) -> None:
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": BROWSER_UA, **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=15):
        pass


MAX_BODY_CHARS = 3500


def _chunks(alerts: list[Alert]) -> list[list[Alert]]:
    """Split so that no message is truncated.

    The body used to be one string cut at 3,500 characters while the cursor
    advanced past every event, so a burst of warnings could push a
    `close_refused` off the end and it was never seen again (O2-02). Alerts are
    delivered in order, in whole pieces, so a partial send is still a correct
    prefix -- which is what lets the cursor advance safely.
    """
    out: list[list[Alert]] = []
    current: list[Alert] = []
    size = 0
    for alert in alerts:
        line = redact(alert)
        if current and size + len(line) + 1 > MAX_BODY_CHARS:
            out.append(current)
            current, size = [], 0
        current.append(alert)
        size += len(line) + 1
    if current:
        out.append(current)
    return out


def send(alerts: list[Alert], *, ntfy_url: str | None = None) -> int:
    """Deliver alerts, in order. Returns how many were ACTUALLY delivered.

    Never raises: a broken sink must not stop trading. The count is a prefix
    length, so the caller can advance its cursor over exactly what arrived.
    """
    if not alerts or not ntfy_url:
        return 0
    sent = 0
    batches = _chunks(alerts)
    for i, batch in enumerate(batches, start=1):
        severities = {a.severity for a in batch}
        if "critical" in severities:
            worst, priority, tags = "critical", "urgent", "rotating_light"
        elif "warning" in severities:
            worst, priority, tags = "warning", "default", "warning"
        else:
            worst, priority, tags = "fills", "low", "chart_with_upwards_trend"
        part = f" ({i}/{len(batches)})" if len(batches) > 1 else ""
        body = "\n".join(redact(a) for a in batch)
        try:
            _post(
                ntfy_url,
                body.encode(),
                {
                    "Title": f"ntx {worst}: {len(batch)} alert(s){part}",
                    "Priority": priority,
                    "Tags": tags,
                },
            )
            sent += len(batch)
        except Exception as e:
            print(f"alert delivery failed after {sent} alert(s): {e}", file=sys.stderr)
            break
    return sent


def heartbeat(url: str | None, *, healthy: bool = True) -> None:
    """Dead-man switch: silence is what the outside service alarms on."""
    if not url:
        return
    target = url if healthy else url.rstrip("/") + "/fail"
    try:
        _post(target, None)
    except Exception as e:
        print(f"heartbeat failed: {e}", file=sys.stderr)


# -------------------------------------------------------------------- main -- #
def main(argv: list[str] | None = None) -> int:
    """One pass. An unexpected error is itself an alert, not a silent exit.

    The unit has no OnFailure= on purpose (F052), so a crash here used to reach a
    human only as a bare dead-man DOWN, period + grace later, with no reason --
    and in the meantime nothing was watched for anyone (O3-04).
    """
    try:
        return _main(argv)
    except Exception as e:
        traceback.print_exc()
        with contextlib.suppress(Exception):
            from ntx.config.settings import load

            settings = load()
            crash = Alert("alerter_crashed", "critical", f"alerter crashed: {type(e).__name__}")
            send([crash], ntfy_url=settings.alert_ntfy_url or None)
            heartbeat(settings.healthcheck_url or None, healthy=False)
        return 1


def _main(argv: list[str] | None = None) -> int:
    from ntx.config.settings import load

    ap = argparse.ArgumentParser(description="ntx alerter + dead-man switch")
    ap.add_argument("--message", help="send this text immediately (systemd OnFailure= hook)")
    # Default is the crash hook's: a unit really did fail. Everything else says
    # what it is -- a rollback is critical, "please install this file" is not.
    ap.add_argument("--kind", default="service_failed", help="alert kind for --message")
    ap.add_argument(
        "--severity", default="critical", choices=("critical", "warning"), help="for --message"
    )
    ap.add_argument("--max-age", type=float, default=120.0, help="snapshot staleness limit (s)")
    ap.add_argument("--dry-run", action="store_true", help="print, do not send")
    args = ap.parse_args(argv)
    settings = load()
    data_dir = settings.data_dir

    expect = [b.strip() for b in settings.alert_bots.split(",") if b.strip()]
    if args.message:
        alerts = [Alert(args.kind, args.severity, args.message, {})]
    else:
        journals = sorted(data_dir.glob("journal*.db"))
        alerts = build_alerts(
            data_dir,
            max_age_s=args.max_age,
            journals=journals,
            expect=expect,
            fills=settings.alert_fills,
        )
        alerts.extend(backup_alerts(data_dir, journals))
        if not expect:
            # Without a declared list, a dead bot is "retired" after 24 h: the
            # alerts stop and the dead-man switch turns green with a position
            # possibly still open (O-02).
            alerts.append(
                Alert(
                    "alert_bots_unset",
                    "critical",
                    "NTX_ALERT_BOTS is empty: no bot is being watched",
                    {},
                )
            )
    # Everything we found goes to stdout -- the systemd journal is the full
    # record and must stay complete. Only the PUSH is deduplicated: that is the
    # channel a human reads, and the one they mute when it repeats itself.
    for a in alerts:
        print(redact(a))
    if args.dry_run:
        return 0
    # Dedup state is written when an alert is CHOSEN, before it is pushed. If the
    # push fails, that state is put back, or the retry sees "already sent" and a
    # KILL never reaches the phone (O3-01).
    dedup_before = _read_dedup_state(data_dir)
    if not args.message:
        pushable = suppress_repeats(alerts, data_dir)
    elif args.kind == "service_failed":
        # The systemd crash hook: every unit failure is its own event. Unchanged.
        pushable = alerts
    else:
        pushable = [a for a in alerts if suppress_repeated_message(a, data_dir)]
    if len(pushable) != len(alerts):
        print(f"(suppressed {len(alerts) - len(pushable)} repeat(s) of conditions already sent)")

    ntfy = settings.alert_ntfy_url or None
    sent = send(pushable, ntfy_url=ntfy)
    # A failed push must not consume the alert: a one-shot critical (guard_KILL,
    # close_refused) would be lost and the next pass would look healthy (O-01).
    # With no sink configured there is nothing to deliver to, and stdout (the
    # systemd journal) is the record, so the cursor may move.
    delivered = sent == len(pushable) if (ntfy and pushable) else True
    # `send` returns a PREFIX length: advance over exactly what arrived, so a
    # partial delivery leaves the rest pending instead of losing it (O2-02).
    advance_cursors(data_dir, alerts if delivered else alerts[:sent])

    fresh = fresh_bots(data_dir, expect, max_age_s=args.max_age)
    healthy = (
        delivered
        and not any(a.severity == "critical" for a in alerts)
        # A pass that watches nothing, or whose bot has gone quiet, is not health.
        and bool(expect)
        and set(fresh) == set(expect)
    )
    # Only the timer's own pass may say "healthy": a deploy notice or a crash hook
    # pinging green between passes masks a timer that has stopped (O3-07). They
    # may still say "failed".
    if not args.message or not healthy:
        heartbeat(settings.healthcheck_url or None, healthy=healthy)
    if not delivered:
        _restore_dedup_state(data_dir, dedup_before)
        print("alerts NOT delivered -- cursor left in place", file=sys.stderr)
        return 1
    # A delivered critical is a SUCCESSFUL run: the job is to report, and it
    # reported. Exiting non-zero here put the timer's unit permanently in
    # `failed` and made its own OnFailure= hook alert about the alerter (F052).
    # The incident travels by push and by the dead-man switch, not by exit code.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
