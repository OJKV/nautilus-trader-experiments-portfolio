"""Phase 3.5 inputs: JSON file logs (docs/11 §6) and the node state snapshot
that the read-only monitor (ntx.ui) reads. Written before the code (R41)."""

from __future__ import annotations

import json

from nautilus_trader.model.data import BarType
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from test_journal_wiring import _trending_bars
from test_live_node import _settings
from test_live_path import _engine

from ntx.live import node
from ntx.strategies.base import NtxStrategy
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy


def test_node_writes_json_file_logs(tmp_path):
    s = _settings(tmp_path)
    cfg = node.node_config(node.plan(s), s)
    assert cfg.logging.log_file_format == "json"
    assert cfg.logging.log_directory.endswith("logs")
    assert cfg.logging.log_file_name.startswith("ntx-S003-live")
    assert cfg.logging.log_file_max_backup_count >= 14


def test_state_path_is_per_strategy_and_environment(tmp_path):
    s = _settings(tmp_path, NTX_VENUE="okx_demo", NTX_EQUITY_OVERRIDE_USD="123.45")
    kw = node.strategy_kwargs(node.plan(s, strategy_id="S900"), s)
    assert kw["state_path"].endswith("state/S900-demo.json")


def _run_with_state(tmp_path):
    inst = TestInstrumentProvider.ethusdt_binance()
    engine = _engine(inst)
    bt = BarType.from_str(f"{inst.id}-1-HOUR-LAST-EXTERNAL")
    engine.add_data(_trending_bars(inst, bt, n=120))
    path = tmp_path / "state" / "S003-demo.json"
    s = DonchianBreakoutStrategy(
        DonchianBreakoutConfig(
            instrument_ids=[str(inst.id)],
            entry_period=20,
            exit_period=10,
            bar_spec="1-HOUR-LAST-EXTERNAL",
            state_path=str(path),
            is_demo=True,
        )
    )
    engine.add_strategy(s)
    engine.run()
    s.write_state()  # what the timer does live
    engine.dispose()
    return json.loads(path.read_text()), s


def test_state_snapshot_matches_the_monitor_contract(tmp_path):
    snap, s = _run_with_state(tmp_path)
    for key in (
        "schema", "ts", "ts_ns", "strategy_id", "trader_id", "venue", "is_demo",
        "trading_enabled", "equity_usd", "unrealized_usd", "positions_unprotected",
        "instruments", "positions", "counters",
    ):  # fmt: skip
        assert key in snap, key
    assert snap["schema"] == 1 and snap["is_demo"] is True
    assert snap["bar_spec"] == "1-HOUR-LAST-EXTERNAL"  # lets the monitor judge bar age
    inst = snap["instruments"][0]
    assert {"id", "last_close", "last_bar_ts_ns", "live_bar_age_s", "bars_seen", "warm"} <= set(
        inst
    )
    assert inst["warm"] is True
    assert snap["counters"]["entries_submitted"] == s.entries_submitted


def test_open_positions_are_in_the_snapshot_with_live_marks(tmp_path):
    snap, s = _run_with_state(tmp_path)
    assert len(snap["positions"]) == len(s.states)
    for p in snap["positions"]:
        assert {"instrument_id", "side", "qty", "entry_price", "mark_price", "stop_price",
                "trailing_stop", "bars_held", "unrealized_usd", "protected", "trade_id"} <= set(p)  # fmt: skip
        assert p["mark_price"] > 0


def test_state_is_written_on_a_timer():
    import inspect

    src = inspect.getsource(NtxStrategy.on_start)
    assert "write_state" in src and "ntx-state" in src


def test_the_snapshot_separates_the_account_balance_from_the_sizing_equity():
    """The monitor showed a sample amount on a DEMO account holding ~a sample amount and the owner
    reasonably asked why. Both numbers are right: the soak sizes every trade as if
    the account held the real $123.45 (NTX_EQUITY_OVERRIDE_USD), because sizing
    from $5,000 would prove nothing about the real account. But the snapshot only
    carried the sizing number, so the monitor could not say so."""
    import inspect

    from ntx.strategies.base import NtxStrategy

    src = inspect.getsource(NtxStrategy.write_state)
    for key in ('"account_equity_usd"', '"sizing_equity_usd"', '"equity_override_usd"'):
        assert key in src, key


def test_the_account_equity_ignores_the_sizing_override():
    from types import SimpleNamespace

    from ntx.strategies.base import NtxStrategy

    fake = SimpleNamespace(
        config=SimpleNamespace(equity_override_usd=123.45, equity_refresh_secs=60),
        instrument_ids=[SimpleNamespace(venue="OKX")],
        equity_hook=lambda: 5012.34,
        _equity_cache=None,
        _equity_cache_ns=0,
        clock=SimpleNamespace(timestamp_ns=lambda: 0),
        log=SimpleNamespace(info=print, warning=print, error=print),
    )
    for name in ("_account_equity_or_none", "_hooked_equity", "_equity_or_none"):
        setattr(fake, name, getattr(NtxStrategy, name).__get__(fake, NtxStrategy))
    assert fake._equity_or_none() == 123.45, "sizing still uses the override"
    assert fake._account_equity_or_none() == 5012.34, "the account shows what it really holds"
