"""Economic health checks -- built before the strategy, on purpose.

The lesson this encodes (docs/13-production-hardening.md §3): a trading bot can
run, log, and raise no exceptions while doing nothing economically useful. In the
field report that produced this rule, every one of 17 bugs was silent -- cron
green, logs filling, 28 days without a trade.

Process health ("is it running?") is table stakes and proves nothing. These
checks ask "is it *trading*, and is it behaving as modelled?"

Pure functions over a Snapshot, so they are testable without a venue, a node, or
a clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["Check", "Severity", "Snapshot", "Thresholds", "evaluate", "worst_severity"]


class Severity(StrEnum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    HIGH = "high"
    CRITICAL = "critical"


_ORDER = [Severity.OK, Severity.INFO, Severity.WARN, Severity.HIGH, Severity.CRITICAL]


@dataclass(frozen=True)
class Check:
    name: str
    severity: Severity
    message: str
    value: float | int | str | None = None


@dataclass(frozen=True)
class Thresholds:
    idle_hours: float = 72.0
    stale_bar_multiple: float = 2.0
    errors_per_hour: int = 20
    rejects_per_5min: int = 5
    min_notional_headroom: float = 1.2
    slippage_model_multiple: float = 2.0
    fee_rate_tolerance: float = 0.10  # 10% drift from the configured rate
    funding_share_of_gross: float = 0.20
    disconnect_seconds: float = 60.0
    heartbeat_seconds: float = 300.0
    disk_pct: float = 85.0


@dataclass
class Snapshot:
    """Everything the checks need. Collected by the MonitorActor."""

    # activity
    hours_since_closed_trade: float | None = None
    seconds_since_last_bar: dict[str, float] = field(default_factory=dict)
    bar_period_seconds: float = 3600.0
    universe_size: int = 0
    paused: bool = False

    # errors
    errors_last_hour: int = 0
    rejects_last_5min: int = 0

    # capital
    equity_usd: float = 0.0
    smallest_min_notional_usd: float = 0.0

    # protection -- the single most important number in the system
    positions_open: int = 0
    positions_unprotected: int = 0
    positions_unclaimed: int = 0

    # model fidelity
    realised_slippage_bps: float | None = None
    model_slippage_bps: float | None = None
    realised_fee_rate: float | None = None
    configured_fee_rate: float | None = None
    funding_paid_usd: float = 0.0
    gross_pnl_usd: float = 0.0

    # connectivity / host
    venue_disconnected_seconds: float = 0.0
    seconds_since_heartbeat: float = 0.0
    disk_used_pct: float = 0.0


def evaluate(s: Snapshot, t: Thresholds | None = None) -> list[Check]:
    """Run every check. Returns one Check per condition, worst-first."""
    t = t or Thresholds()
    out: list[Check] = []

    def add(name: str, sev: Severity, msg: str, value: object = None) -> None:
        out.append(Check(name, sev, msg, value))  # type: ignore[arg-type]

    # --- the invariant: nothing may hold a position without a venue-side stop --
    if s.positions_unprotected > 0:
        add(
            "positions_unprotected",
            Severity.CRITICAL,
            f"{s.positions_unprotected} position(s) have no venue-side stop",
            s.positions_unprotected,
        )
    else:
        add("positions_unprotected", Severity.OK, "all positions protected", 0)

    if s.positions_unclaimed > 0:
        add(
            "positions_unclaimed",
            Severity.CRITICAL,
            f"{s.positions_unclaimed} position(s) claimed by no strategy -- flatten",
            s.positions_unclaimed,
        )

    # --- is it actually trading? ---
    if s.paused:
        add("activity", Severity.INFO, "paused by operator")
    elif s.universe_size == 0:
        add("activity", Severity.HIGH, "tradable universe is empty (preflight dropped everything)")
    elif s.hours_since_closed_trade is None:
        add("activity", Severity.INFO, "no closed trades yet")
    elif s.hours_since_closed_trade > t.idle_hours:
        add(
            "activity",
            Severity.HIGH,
            f"economically idle: no closed trade in {s.hours_since_closed_trade:.0f}h",
            s.hours_since_closed_trade,
        )
    else:
        add(
            "activity",
            Severity.OK,
            f"last close {s.hours_since_closed_trade:.1f}h ago",
            s.hours_since_closed_trade,
        )

    # --- is data arriving? ---
    stale = {
        inst: age
        for inst, age in s.seconds_since_last_bar.items()
        if age > s.bar_period_seconds * t.stale_bar_multiple
    }
    if stale:
        worst = max(stale.values())
        add(
            "stale_data",
            Severity.HIGH,
            f"{len(stale)} instrument(s) stale, worst {worst / 60:.0f}min",
            worst,
        )
    elif s.seconds_since_last_bar:
        add("stale_data", Severity.OK, "all instruments fresh")

    # --- errors ---
    if s.rejects_last_5min >= t.rejects_per_5min:
        add(
            "reject_storm",
            Severity.CRITICAL,
            f"{s.rejects_last_5min} rejects in 5min -- halt; usually an instrument-spec bug",
            s.rejects_last_5min,
        )
    if s.errors_last_hour > t.errors_per_hour:
        add(
            "error_rate",
            Severity.HIGH,
            f"{s.errors_last_hour} errors in the last hour",
            s.errors_last_hour,
        )

    # --- can we even trade at this equity? ---
    if s.smallest_min_notional_usd > 0:
        needed = s.smallest_min_notional_usd * t.min_notional_headroom
        if s.equity_usd < needed:
            add(
                "capital",
                Severity.CRITICAL,
                f"equity ${s.equity_usd:.2f} below min notional x{t.min_notional_headroom} "
                f"(${needed:.2f}) -- cannot trade",
                s.equity_usd,
            )
        else:
            add("capital", Severity.OK, f"equity ${s.equity_usd:.2f}", s.equity_usd)

    # --- is reality matching the backtest's assumptions? ---
    if s.realised_slippage_bps is not None and s.model_slippage_bps:
        ratio = s.realised_slippage_bps / s.model_slippage_bps
        if ratio > t.slippage_model_multiple:
            add(
                "slippage_drift",
                Severity.HIGH,
                f"realised slippage {ratio:.1f}x the model -- backtest is optimistic",
                ratio,
            )
        else:
            add("slippage_drift", Severity.OK, f"slippage {ratio:.2f}x model", ratio)

    if s.realised_fee_rate is not None and s.configured_fee_rate:
        drift = abs(s.realised_fee_rate - s.configured_fee_rate) / abs(s.configured_fee_rate)
        if drift > t.fee_rate_tolerance:
            add(
                "fee_drift",
                Severity.HIGH,
                f"realised fee rate differs from configured by {drift:.0%} -- "
                f"refetch from the venue",
                drift,
            )
        else:
            add("fee_drift", Severity.OK, f"fee rate within {drift:.1%}", drift)

    if s.gross_pnl_usd > 0:
        share = s.funding_paid_usd / s.gross_pnl_usd
        if share > t.funding_share_of_gross:
            add("funding_drag", Severity.WARN, f"funding is {share:.0%} of gross PnL", share)

    # --- connectivity & host ---
    if s.venue_disconnected_seconds > t.disconnect_seconds:
        add(
            "venue_connection",
            Severity.HIGH,
            f"venue disconnected {s.venue_disconnected_seconds:.0f}s -- entries vetoed",
            s.venue_disconnected_seconds,
        )
    if s.seconds_since_heartbeat > t.heartbeat_seconds:
        add(
            "heartbeat",
            Severity.HIGH,
            f"no heartbeat for {s.seconds_since_heartbeat / 60:.0f}min",
            s.seconds_since_heartbeat,
        )
    if s.disk_used_pct > t.disk_pct:
        add("disk", Severity.WARN, f"disk {s.disk_used_pct:.0f}% used", s.disk_used_pct)

    out.sort(key=lambda c: _ORDER.index(c.severity), reverse=True)
    return out


def worst_severity(checks: list[Check]) -> Severity:
    return max((c.severity for c in checks), key=_ORDER.index, default=Severity.OK)
