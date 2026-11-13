"""Backtest runner.

Honesty is the design goal here, not speed. Every cost that eats a
trend-following edge is modelled: taker fees both sides, slippage, and funding.
See docs/09-backtest-methodology.md §3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money
from nautilus_trader.trading.strategy import Strategy

from ntx.config.universe import BY_BASE, UNIVERSE
from ntx.data.catalog import _TF_SPEC
from ntx.data.instruments import build_instrument
from ntx.data.read import open_catalog, read_bars

# OKX Lv1, fetched from the venue 2026-09-15. The backtest uses the worst case
# (taker both sides) -- documented fee rates have been wrong by 6-12x elsewhere,
# so the live node refetches from /api/v5/account/trade-fee (R16).
OKX_TAKER_FEE = Decimal("0.0005")
OKX_MAKER_FEE = Decimal("0.0002")


@dataclass
class BacktestSpec:
    strategy_cls: type[Strategy]
    config_cls: type
    config_kwargs: dict = field(default_factory=dict)
    assets: tuple[str, ...] = tuple(a.base for a in UNIVERSE)
    timeframe: str = "1h"
    start: str | None = None
    end: str | None = None
    starting_equity_usd: float = 10_000.0
    # Slippage is modelled as an ADDITION TO THE TAKER FEE, in basis points of
    # notional per side. nautilus's FillModel can only slip by one tick, which
    # for BTC is 0.0132 bps -- about 150x too small to matter (F019). Folding it
    # into the fee is first-order correct for market orders and, unlike a
    # post-hoc adjustment, it affects the equity path and therefore compounding.
    slippage_bps_per_side: float = 2.0
    fee_multiplier: float = 1.0  # cost-stress knob for docs/09 §7


@dataclass
class BacktestResult:
    spec: BacktestSpec
    bars: int
    account_report: pd.DataFrame
    fills_report: pd.DataFrame
    positions_report: pd.DataFrame
    strategy_counters: dict
    equity_curve: pd.Series = field(default_factory=pd.Series)

    @property
    def trades(self) -> int:
        return len(self.positions_report)

    def summary(self) -> str:
        n = self.trades
        lines = [
            f"bars={self.bars:,}  trades={n}  "
            f"assets={len(self.spec.assets)}  tf={self.spec.timeframe}",
            f"counters={self.strategy_counters}",
        ]
        if n and "realized_pnl" in self.positions_report.columns:
            pnl = pd.to_numeric(self.positions_report["realized_pnl"], errors="coerce").dropna()
            if len(pnl):
                wins = (pnl > 0).sum()
                gross_win = pnl[pnl > 0].sum()
                gross_loss = -pnl[pnl < 0].sum()
                lines.append(
                    f"net_pnl=${pnl.sum():,.2f}  win_rate={wins / len(pnl):.1%}  "
                    f"profit_factor={(gross_win / gross_loss) if gross_loss else float('inf'):.2f}  "
                    f"avg=${pnl.mean():,.2f}"
                )
        return "\n".join(lines)


def run(spec: BacktestSpec, data_dir: Path) -> BacktestResult:
    catalog = open_catalog(data_dir)

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="NTX-BT-001",
            logging=LoggingConfig(bypass_logging=True),
        )
    )
    venue = None
    total_bars = 0
    instruments = []

    # Effective taker cost per side = venue taker fee + modelled slippage.
    effective_taker = (
        OKX_TAKER_FEE + Decimal(str(spec.slippage_bps_per_side / 10_000.0))
    ) * Decimal(str(spec.fee_multiplier))

    for base in spec.assets:
        asset = BY_BASE[base.upper()]
        inst_id = asset.nautilus_id
        try:
            bars = read_bars(catalog, inst_id, spec.timeframe, start=spec.start, end=spec.end)
        except KeyError:
            continue
        if not bars:
            continue
        instrument = next(i for i in catalog.instruments() if str(i.id) == inst_id)
        # Rebuild the instrument with the cost-loaded fee. The catalog copy
        # carries the raw venue fee; the backtest must charge fee + slippage.
        instrument = build_instrument(
            instrument.info,
            maker_fee=OKX_MAKER_FEE * Decimal(str(spec.fee_multiplier)),
            taker_fee=effective_taker,
        )
        instruments.append(instrument)
        venue = instrument.id.venue
        total_bars += len(bars)

    if not instruments:
        raise RuntimeError("no instruments with data for this spec")

    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,  # matches OKX posMode=net_mode
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(spec.starting_equity_usd, USDT)],
        # No FillModel slippage: it can only slip by ONE TICK, which for BTC is
        # 0.0132 bps -- roughly 150x too small to matter (F019). Slippage is
        # charged through the taker fee instead, so it affects compounding.
        fill_model=FillModel(prob_fill_on_limit=1.0, random_seed=42),
        bar_execution=True,  # fill market orders from bars
    )

    for instrument in instruments:
        engine.add_instrument(instrument)
        asset = next(a for a in UNIVERSE if a.nautilus_id == str(instrument.id))
        engine.add_data(
            read_bars(catalog, asset.nautilus_id, spec.timeframe, start=spec.start, end=spec.end)
        )

    # The strategy's `bar_spec` and the spec's `timeframe` are two independent
    # settings that MUST agree. When they did not, the strategy subscribed to
    # 1-HOUR bars while 4-HOUR data was loaded, so `on_bar` never fired and the
    # run reported a clean "0 trades" with no error at all (F016). Derived here
    # so they cannot diverge.
    kwargs = dict(spec.config_kwargs)
    kwargs.setdefault("bar_spec", f"{_TF_SPEC[spec.timeframe]}-LAST-EXTERNAL")
    cfg = spec.config_cls(
        instrument_ids=[str(i.id) for i in instruments],
        **kwargs,
    )
    strategy = spec.strategy_cls(config=cfg)
    engine.add_strategy(strategy)
    engine.run()

    # A run that delivered no bars is a configuration error, not a result.
    # Reporting "0 trades" for it would be indistinguishable from a strategy
    # that legitimately never traded (F016).
    delivered = sum(strategy.bars_seen.values())
    if delivered == 0:
        raise RuntimeError(
            f"strategy received 0 bars: it subscribed to '{cfg.bar_spec}' while "
            f"{total_bars:,} bars of '{spec.timeframe}' data were loaded -- "
            f"bar_spec and timeframe disagree"
        )

    result = BacktestResult(
        spec=spec,
        bars=total_bars,
        account_report=engine.trader.generate_account_report(venue),
        fills_report=engine.trader.generate_order_fills_report(),
        positions_report=engine.trader.generate_positions_report(),
        equity_curve=(
            pd.Series(
                [v for _, v in strategy.equity_curve],
                index=pd.to_datetime([ts for ts, _ in strategy.equity_curve], utc=True),
                name="equity",
            )
            if strategy.equity_curve
            else pd.Series(dtype=float, name="equity")
        ),
        strategy_counters={
            "entries_attempted": strategy.entries_attempted,
            "entries_submitted": strategy.entries_submitted,
            "rejects": dict(strategy.rejects),
            # Caps that shrank an order instead of refusing it (R40).
            "size_caps": dict(getattr(strategy, "size_caps", {})),
            "exits": dict(strategy.exit_counts),
            "unprotected_events": strategy.unprotected_events,
            "entries_refused": strategy.entries_refused,
            "stops_refused": strategy.stops_refused,
            "entry_skips": dict(strategy.entry_skips),
            "filtered_out": getattr(strategy, "filtered_out", 0),
        },
    )
    engine.dispose()
    return result
