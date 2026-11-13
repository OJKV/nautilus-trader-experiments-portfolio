"""Phase 0.5 smoke test: prove the installed engine can run a backtest.

Deliberately minimal -- asserts the toolchain works, not that any strategy is
good. Synthetic bars, no venue, no network.
"""

from __future__ import annotations

from itertools import pairwise

import pandas as pd
import pytest
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money
from nautilus_trader.test_kit.providers import TestInstrumentProvider

HOUR_NS = 3_600_000_000_000


def _synthetic_bars(instrument, bar_type: BarType, n: int = 200) -> list[Bar]:
    """Up-trend then down-trend, so a crossover strategy would trade both ways."""
    bars: list[Bar] = []
    price = 2_000.0
    start = int(pd.Timestamp("2024-01-01", tz="UTC").value)
    for i in range(n):
        price *= 1.001 if i < n // 2 else 0.999
        ts = start + i * HOUR_NS
        bars.append(
            Bar(
                bar_type=bar_type,
                open=instrument.make_price(price),
                high=instrument.make_price(price * 1.002),
                low=instrument.make_price(price * 0.998),
                close=instrument.make_price(price),
                volume=instrument.make_qty(10),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return bars


@pytest.fixture
def instrument():
    return TestInstrumentProvider.ethusdt_binance()


@pytest.fixture
def engine(instrument) -> BacktestEngine:
    eng = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="SMOKE-001",
            logging=LoggingConfig(bypass_logging=True),
        )
    )
    eng.add_venue(
        venue=instrument.id.venue,  # venue must match the instrument
        oms_type=OmsType.NETTING,  # matches OKX posMode=net_mode
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(10_000, USDT)],
    )
    eng.add_instrument(instrument)
    return eng


def test_engine_runs_over_synthetic_bars(engine: BacktestEngine, instrument) -> None:
    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    bars = _synthetic_bars(instrument, bar_type)

    engine.add_data(bars)
    engine.run()

    report = engine.trader.generate_account_report(instrument.id.venue)
    assert report is not None
    engine.dispose()


def test_bars_are_ordered_and_well_formed(instrument) -> None:
    """Cheap data-integrity analogue of the Phase 1 checks (OHLC sanity)."""
    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    bars = _synthetic_bars(instrument, bar_type)

    assert len({b.ts_event for b in bars}) == len(bars), "duplicate timestamps"
    assert all(b.ts_event < n.ts_event for b, n in pairwise(bars))
    for b in bars:
        assert b.low <= b.open <= b.high
        assert b.low <= b.close <= b.high
        assert b.volume > 0


def test_pct_params_are_denominated() -> None:
    """R4 guard: every percentage setting names its denominator.

    Prevents the 5x-leverage class of bug where a price move and a return on
    margin are silently interchanged (docs/01-architecture.md §5).
    """
    from ntx.config.settings import Settings

    bad = [
        alias
        for name, f in Settings.model_fields.items()
        if (alias := (f.alias or name)).upper().endswith("_PCT")
        and not alias.upper().endswith(("_PRICE_PCT", "_ROI_PCT", "_EQUITY_PCT"))
    ]
    assert not bad, f"undenominated percent settings: {bad}"


def test_venue_capabilities_declared() -> None:
    """R5 guard: capabilities are declared flags, not assumptions in code."""
    from ntx.config import venues

    okx = venues.get("OKX")
    assert okx.native_trailing_stop is True
    # F007: OKXExecutionClient submits neither bracket nor OCO orders.
    assert okx.attaches_stop_at_entry is False
    assert okx.fee_endpoint, "fees must be fetched from the venue, never hardcoded (R16)"


def test_unimplemented_venue_raises_clearly() -> None:
    from ntx.config import venues

    with pytest.raises(NotImplementedError, match="ADR-0010"):
        venues.get("BINANCE")


def test_min_notional_okx_contract_maths() -> None:
    """OKX sizes in contracts: notional = min_size * ct_val * price."""
    from ntx.config.venues import min_notional_usd

    assert round(min_notional_usd(price=76_700.0, min_size=0.01, ct_val=0.01), 2) == 7.67
    # Binance/Bybit style: base-unit min qty plus a separate notional floor.
    assert min_notional_usd(price=2_465.0, min_size=0.001, venue_min_notional=20.0) == 20.0


def test_strategy_and_exit_pct_params_are_denominated() -> None:
    """R4 for the strategy and exit configs too (review 2026-10-11 F-02).

    The guard above scans only `Settings`; `target_roi_pct` sat in
    NtxStrategyConfig and ExitConfig, invisible to it.
    """
    import dataclasses

    from ntx.strategies import (
        base,
        cross_sectional_momentum,
        donchian_breakout,
        donchian_fast,
        exits,
        ma_cross_trend,
        mean_reversion,
        random_entry,
        rehearsal,
        volatility_breakout,
    )  # fmt: skip

    names: set[str] = {f.name for f in dataclasses.fields(exits.ExitConfig)}
    for cfg in (
        base.NtxStrategyConfig, cross_sectional_momentum.CrossSectionalMomentumConfig,
        donchian_breakout.DonchianBreakoutConfig, donchian_fast.FastDonchianConfig,
        ma_cross_trend.MACrossTrendConfig, mean_reversion.MeanReversionConfig,
        random_entry.RandomEntryConfig, rehearsal.RehearsalConfig,
        volatility_breakout.VolatilityBreakoutConfig,
    ):  # fmt: skip
        names |= set(getattr(cfg, "__annotations__", {})) | set(
            getattr(cfg, "__struct_fields__", ())
        )
    bad = sorted(
        n for n in names
        if n.endswith("_pct") and not n.endswith(("_price_pct", "_roi_pct", "_equity_pct"))
    )  # fmt: skip
    assert not bad, f"undenominated percent parameters: {bad}"
