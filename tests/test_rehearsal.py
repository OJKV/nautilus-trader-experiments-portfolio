"""S900 plumbing rehearsal: enter once per instrument, exit after N bars."""

from __future__ import annotations

from nautilus_trader.model.data import BarType
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from test_journal_wiring import _trending_bars
from test_live_path import _engine

from ntx.strategies.registry import REHEARSAL_ONLY, get
from ntx.strategies.rehearsal import RehearsalConfig, RehearsalStrategy


def _run(**kw):
    inst = TestInstrumentProvider.ethusdt_binance()
    engine = _engine(inst)
    bt = BarType.from_str(f"{inst.id}-1-HOUR-LAST-EXTERNAL")
    engine.add_data(_trending_bars(inst, bt, n=60))
    s = RehearsalStrategy(
        RehearsalConfig(instrument_ids=[str(inst.id)], bar_spec="1-HOUR-LAST-EXTERNAL", **kw)
    )
    engine.add_strategy(s)
    engine.run()
    rep = engine.trader.generate_positions_report()
    engine.dispose()
    return s, rep


def test_enters_exactly_once_and_exits_on_time():
    s, rep = _run(hold_bars=3)
    assert s.entries_submitted == 1
    assert len(rep) == 1
    assert s.exit_counts == {"max_bars": 1} or sum(s.exit_counts.values()) == 1


def test_registered_and_marked_demo_only():
    assert get("S900")[0] is RehearsalStrategy
    assert "S900" in REHEARSAL_ONLY


def test_warmup_is_short():
    s, _ = _run()
    assert s.warmup_bars() <= 16


def test_history_does_not_consume_the_one_shot_entry():
    """F033: the first demo rehearsal never traded. The one-shot trigger was
    marked 'done' inside on_bar_signal, which also runs on warm-up history."""
    from test_live_path import _HistoryFirst  # noqa: F401  (pattern reference)

    inst = TestInstrumentProvider.ethusdt_binance()
    engine = _engine(inst)
    bt = BarType.from_str(f"{inst.id}-1-HOUR-LAST-EXTERNAL")
    bars = _trending_bars(inst, bt, n=60)
    hist, live = bars[:30], bars[30:]
    engine.add_data(live)

    class HistFirst(RehearsalStrategy):
        def on_start(self):
            super().on_start()
            for b in hist:
                self.on_historical_data(b)

    s = HistFirst(
        RehearsalConfig(instrument_ids=[str(inst.id)], bar_spec="1-HOUR-LAST-EXTERNAL", hold_bars=3)
    )
    engine.add_strategy(s)
    engine.run()
    engine.dispose()
    assert s.entries_submitted == 1
