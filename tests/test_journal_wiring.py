"""The strategy must persist what it did to the journal (F026).

Written BEFORE the fix, per R41.

Why this matters operationally: OKX needs a VPN from Kazakhstan, so the user
will stop the tunnel between sessions. Anything that exists only in venue state
or in process memory becomes unreviewable the moment the VPN drops. The journal
is local SQLite -- if it is written, every order, fill and exit stays reviewable
offline forever.
"""

from __future__ import annotations

import pandas as pd
import pytest
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from ntx.obs.journal import Journal
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy

H = 3_600_000_000_000


def _trending_bars(inst, bar_type, n=400):
    """Up-then-down ramp steep enough that a breakout actually triggers.

    The per-bar drift MUST exceed the intrabar high offset. With a 0.4% ramp and
    highs 0.4% above the close, each close lands exactly ON the previous bar's
    high, so `close > channel_high` is never true and the strategy correctly
    never trades -- a pathological fixture that looks like a broken strategy.
    """
    bars, price = [], 2000.0
    drift, wick = 1.010, 0.002  # 1.0% drift vs 0.2% wick
    start = int(pd.Timestamp("2024-01-01", tz="UTC").value)
    for i in range(n):
        price *= drift if i < n // 2 else (2.0 - drift)
        ts = start + i * H
        bars.append(
            Bar(
                bar_type=bar_type,
                open=inst.make_price(price),
                high=inst.make_price(price * (1 + wick)),
                low=inst.make_price(price * (1 - wick)),
                close=inst.make_price(price),
                volume=inst.make_qty(10),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return bars


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path / "journal.db")


def _run(journal: Journal, inst) -> DonchianBreakoutStrategy:
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="JRNL-001", logging=LoggingConfig(bypass_logging=True)
        )
    )
    engine.add_venue(
        venue=inst.id.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(100_000, USDT)],
        fill_model=FillModel(prob_fill_on_limit=1.0, random_seed=42),
        bar_execution=True,
    )
    engine.add_instrument(inst)
    bar_type = BarType.from_str(f"{inst.id}-1-HOUR-LAST-EXTERNAL")
    engine.add_data(_trending_bars(inst, bar_type))
    strategy = DonchianBreakoutStrategy(
        DonchianBreakoutConfig(
            instrument_ids=[str(inst.id)],
            entry_period=20,
            exit_period=10,
            bar_spec="1-HOUR-LAST-EXTERNAL",
            journal_path=str(journal.path),
        )
    )
    engine.add_strategy(strategy)
    engine.run()
    engine.dispose()
    return strategy


def test_strategy_records_trades_to_the_journal(journal):
    inst = TestInstrumentProvider.ethusdt_binance()
    strategy = _run(journal, inst)
    assert strategy.entries_submitted > 0, "test setup: the strategy must have traded"

    closed = journal.closed_trades()
    assert closed, "journal recorded NO trades -- nothing would survive the VPN dropping"
    row = closed[0]
    for col in (
        "instrument_id",
        "side",
        "entry_price",
        "exit_price",
        "exit_reason",
        "pnl_net_usd",
        "strategy_id",
    ):
        assert row[col] is not None, f"journal trade is missing {col}"


def test_strategy_records_entry_attempts(journal):
    """R19: cooldowns key on attempts, so attempts must be persisted."""
    inst = TestInstrumentProvider.ethusdt_binance()
    _run(journal, inst)
    assert journal.seconds_since_last_attempt() is not None, "no attempts recorded"


def test_journal_survives_process_restart(tmp_path):
    """The whole point: reopen the file and the history is still there."""
    inst = TestInstrumentProvider.ethusdt_binance()
    path = tmp_path / "journal.db"
    _run(Journal(path), inst)
    reopened = Journal(path)
    assert reopened.closed_trades(), "history did not persist across reopen"


def test_equity_samples_are_recorded(journal):
    inst = TestInstrumentProvider.ethusdt_binance()
    _run(journal, inst)
    assert journal.equity_high_water_mark() is not None, "no equity samples recorded"


def test_journal_is_optional(tmp_path):
    """Backtests and sweeps must not be forced to write a journal."""
    inst = TestInstrumentProvider.ethusdt_binance()
    s = DonchianBreakoutStrategy(
        DonchianBreakoutConfig(instrument_ids=[str(inst.id)], entry_period=20, exit_period=10)
    )
    assert s.journal is None
