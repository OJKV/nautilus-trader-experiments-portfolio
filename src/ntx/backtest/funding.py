"""Funding cost attribution for perpetual positions.

R7 requires funding in every backtest, and it was missing: the data was
downloaded in Phase 1 and never applied (F025). Perpetual funding settles every
8h; **positive rate means longs pay shorts**, so a long/short strategy pays on
its long legs and receives on its short legs.

Computed per closed position from the funding series rather than folded into the
fee, because the amount depends on holding time and direction -- a flat per-trade
charge would be wrong in both.

Limitation, stated plainly: this is applied **post hoc** to the realised trade
list, so it corrects totals and per-trade attribution but does not compound
through the equity path. At ~0.05% of notional per trade against 0.14% of
already-modelled costs, that second-order error is small -- but it is an error,
not zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from ntx.config.universe import UNIVERSE
from ntx.data.binance_dumps import parse_funding

MS_TO_NS = 1_000_000


@dataclass
class FundingImpact:
    trades_charged: int
    total_funding_usd: float  # negative = the strategy paid out
    paid_usd: float
    received_usd: float
    pct_of_gross_pnl: float
    mean_per_trade_usd: float

    def summary(self) -> str:
        return (
            f"funding: net ${self.total_funding_usd:+,.2f} "
            f"(paid ${self.paid_usd:,.2f}, received ${self.received_usd:,.2f}) "
            f"over {self.trades_charged} trades, "
            f"{self.pct_of_gross_pnl:+.1f}% of gross PnL, "
            f"${self.mean_per_trade_usd:+.4f}/trade"
        )


def load_funding_series(raw_dir: Path) -> dict[str, pd.Series]:
    """{nautilus instrument id: Series of 8h funding rates indexed by UTC time}."""
    out: dict[str, pd.Series] = {}
    for asset in UNIVERSE:
        d = raw_dir / "fundingRate" / asset.binance_symbol
        if not d.exists():
            continue
        rows: list[tuple[int, float]] = []
        for z in sorted(d.glob("*.zip")):
            rows += [(f.calc_time, f.rate) for f in parse_funding(z.read_bytes())]
        if not rows:
            continue
        rows.sort()
        idx = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True)
        out[asset.nautilus_id] = pd.Series([r[1] for r in rows], index=idx).sort_index()
    return out


def apply(
    positions: pd.DataFrame,
    funding: dict[str, pd.Series],
    multipliers: dict[str, float],
) -> tuple[pd.Series, FundingImpact]:
    """Funding cost per closed position, in quote currency.

    `multipliers` maps instrument id -> contract value (ct_val), since OKX
    quantities are contracts and notional = qty * ct_val * price.
    Returns (per-trade funding series aligned to `positions`, aggregate impact).
    """
    from ntx.backtest.metrics import money_to_float

    if positions.empty:
        return pd.Series(dtype=float), FundingImpact(0, 0.0, 0.0, 0.0, 0.0, 0.0)

    costs: list[float] = []
    for _, row in positions.iterrows():
        inst = str(row.get("instrument_id", ""))
        series = funding.get(inst)
        opened, closed = row.get("ts_opened"), row.get("ts_closed")
        if series is None or pd.isna(opened) or pd.isna(closed):
            costs.append(0.0)
            continue

        t0 = pd.to_datetime(opened, utc=True)
        t1 = pd.to_datetime(closed, utc=True)
        window = series.loc[(series.index > t0) & (series.index <= t1)]
        if window.empty:
            costs.append(0.0)
            continue

        qty = abs(float(row.get("peak_qty") or 0.0))
        price = float(row.get("avg_px_open") or 0.0)
        notional = qty * multipliers.get(inst, 1.0) * price
        # 'entry' is the side that OPENED the position.
        is_long = str(row.get("entry", "")).upper() == "BUY"
        rate_sum = float(window.sum())
        # positive rate: longs pay. Cost is negative for a long.
        costs.append(-rate_sum * notional if is_long else rate_sum * notional)

    s = pd.Series(costs, index=positions.index, name="funding_usd")
    gross = money_to_float(positions["realized_pnl"]).dropna().sum()
    paid = float(-s[s < 0].sum())
    received = float(s[s > 0].sum())
    return s, FundingImpact(
        trades_charged=int((s != 0).sum()),
        total_funding_usd=float(s.sum()),
        paid_usd=paid,
        received_usd=received,
        pct_of_gross_pnl=float(s.sum() / abs(gross) * 100.0) if gross else 0.0,
        mean_per_trade_usd=float(s.mean()),
    )
