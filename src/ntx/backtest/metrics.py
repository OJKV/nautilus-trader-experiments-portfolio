"""Backtest metrics.

Reported honestly and with the red-flag table in mind (docs/09 §8): a Sharpe
above 2 on 1h crypto trend-following is a bug, not an edge, and the benchmark
that matters most is whether the strategy beats buy-and-hold on a
**risk-adjusted** basis rather than on raw return.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

HOURS_PER_YEAR = 24 * 365


def money_to_float(series: pd.Series) -> pd.Series:
    """Parse nautilus money strings like '-103.33213584 USDT' to floats.

    `pd.to_numeric` silently yields all-NaN on these, which made the runner's
    own summary print nothing -- a monitoring bug of exactly the kind
    docs/13 §1 pattern 12 warns about.
    """
    return pd.to_numeric(
        series.astype(str).str.replace(r"[^\d.\-eE]", "", regex=True), errors="coerce"
    )


@dataclass
class Metrics:
    trades: int
    net_pnl: float  # realised only
    return_pct: float  # MARK-TO-MARKET, from the equity curve
    realized_return_pct: float  # realised PnL / starting equity
    unrealized_at_end_pct: float  # share of final equity that is not locked in
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    payoff_ratio: float
    largest_win: float
    largest_loss: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    exposure_pct: float
    fees_paid: float

    def summary(self) -> str:
        return (
            f"trades={self.trades}  mtm={self.return_pct:+.1f}%  "
            f"realised={self.realized_return_pct:+.1f}%  "
            f"unrealised_at_end={self.unrealized_at_end_pct:.0f}%  "
            f"win={self.win_rate:.1%}  PF={self.profit_factor:.2f}  "
            f"payoff={self.payoff_ratio:.2f}\n"
            f"maxDD={self.max_drawdown_pct:.1f}%  Sharpe={self.sharpe:.2f}  "
            f"Sortino={self.sortino:.2f}  fees=${self.fees_paid:,.2f}  "
            f"largest win/loss=${self.largest_win:,.0f}/${self.largest_loss:,.0f}"
        )

    def red_flags(self) -> list[str]:
        """Conditions that mean 'bug', not 'edge' (docs/09 §8)."""
        flags = []
        if self.sharpe > 2.0:
            flags.append(f"Sharpe {self.sharpe:.2f} > 2.0 -- suspect lookahead or missing costs")
        if self.win_rate > 0.60:
            flags.append(f"win rate {self.win_rate:.0%} > 60% for trend-following -- check exits")
        if 0 < self.max_drawdown_pct < 10:
            flags.append(f"maxDD {self.max_drawdown_pct:.1f}% < 10% -- costs may be missing")
        if self.trades and self.fees_paid == 0:
            flags.append("zero fees recorded -- fee model not applied")
        if self.unrealized_at_end_pct > 25:
            flags.append(
                f"{self.unrealized_at_end_pct:.0f}% of final equity is UNREALISED on "
                f"open positions -- not locked in, and sensitive to the cut-off date"
            )
        return flags


PERIODS_PER_YEAR = {"15m": 4 * 24 * 365, "1h": 24 * 365, "4h": 6 * 365, "1d": 365}


def compute(
    positions: pd.DataFrame,
    equity_curve: pd.Series,
    starting_equity: float,
    bars: int,
    timeframe: str = "1h",
) -> Metrics:
    """Metrics from closed positions plus a mark-to-market equity curve.

    The equity curve must be the sampled `portfolio.equity()` series, NOT the
    account report: the account report only updates on fills, so it omits
    unrealised PnL and understates drawdown (F015).
    """
    # Trade statistics need closed positions; the equity-derived RISK metrics do
    # not. Returning all zeros here hid a real mark-to-market drawdown whenever a
    # window had no closed trade yet (M-01).
    empty = positions.empty
    pnl = pd.Series(dtype=float) if empty else money_to_float(positions["realized_pnl"]).dropna()
    fees = 0.0 if empty else money_to_float(positions["commissions"]).dropna().abs().sum()
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()

    equity = equity_curve.dropna() if len(equity_curve) else pd.Series(dtype=float)
    if len(equity) > 1:
        running_max = equity.cummax()
        max_dd = abs(((equity - running_max) / running_max * 100.0).min())
        rets = equity.pct_change().dropna()
        scale = math.sqrt(PERIODS_PER_YEAR.get(timeframe, 24 * 365))
        if len(rets) > 1 and rets.std() > 0:
            sharpe = rets.mean() / rets.std() * scale
            downside = rets[rets < 0].std()
            sortino = (rets.mean() / downside * scale) if downside and downside > 0 else 0.0
        else:
            sharpe = sortino = 0.0
    else:
        max_dd = sharpe = sortino = 0.0

    held = (
        0.0
        if empty
        else money_to_float(positions.get("duration_ns", pd.Series(dtype=float))).dropna().sum()
    )
    exposure = (held / (bars * 3.6e12) * 100.0) if bars else 0.0

    # Mark-to-market return from the equity curve. Using realised PnL over the
    # STARTING equity sums dollars earned at different equity levels and divides
    # by the initial one -- which under compounding is meaningless, and reported
    # 2023 as -10% while its equity curve rose 69% (F021).
    final_equity = float(equity.iloc[-1]) if len(equity) else starting_equity
    first_equity = float(equity.iloc[0]) if len(equity) else starting_equity
    mtm_return = (final_equity / first_equity - 1.0) * 100.0 if first_equity else 0.0

    # How much of the finishing equity is still riding on open positions?
    realised_equity = starting_equity + float(pnl.sum())
    unrealised = max(0.0, final_equity - realised_equity)
    unrealised_share = (unrealised / final_equity * 100.0) if final_equity > 0 else 0.0

    return Metrics(
        trades=len(pnl),
        net_pnl=float(pnl.sum()) if len(pnl) else 0.0,
        return_pct=float(mtm_return),
        realized_return_pct=float(pnl.sum() / starting_equity * 100.0) if len(pnl) else 0.0,
        unrealized_at_end_pct=float(unrealised_share),
        win_rate=float(len(wins) / len(pnl)) if len(pnl) else 0.0,
        profit_factor=float(gross_win / gross_loss) if gross_loss else float("inf"),
        avg_win=float(wins.mean()) if len(wins) else 0.0,
        avg_loss=float(losses.mean()) if len(losses) else 0.0,
        payoff_ratio=float(abs(wins.mean() / losses.mean())) if len(wins) and len(losses) else 0.0,
        largest_win=float(pnl.max()) if len(pnl) else 0.0,
        largest_loss=float(pnl.min()) if len(pnl) else 0.0,
        max_drawdown_pct=float(max_dd),
        sharpe=float(sharpe),
        sortino=float(sortino),
        exposure_pct=float(min(exposure, 100.0)),
        fees_paid=float(fees),
    )
