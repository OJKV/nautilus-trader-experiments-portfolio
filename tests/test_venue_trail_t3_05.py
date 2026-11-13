"""T3-05: the venue-trail table compared a KILL-truncated "off" row with full ON rows.

A row a loss guard ended early is not comparable with one that traded the whole
window. `--guards-off` must really turn both guards off (R40: prove the knob was
turned), and each row must say whether a KILL ended it.
"""

from __future__ import annotations

import importlib
import json
import sys

import pytest

from ntx.experiments import s003_gate
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig


@pytest.fixture
def vt(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"equity_usd": 1000.0, "assets": ["BTC"]}))
    monkeypatch.setattr(s003_gate, "PRIVATE_CONFIG", config)
    monkeypatch.delitem(sys.modules, "ntx.experiments.venue_trail", raising=False)
    return importlib.import_module("ntx.experiments.venue_trail")


def test_guards_off_really_disables_both_guards(vt):
    spec = vt._spec(0.0, ("2020-01-01", "2020-02-01"), 100.0, guards_off=True)
    # the knob reaches a real config, not just a dict
    cfg = DonchianBreakoutConfig(instrument_ids=["BTCUSDT-PERP.BINANCE"], **spec.config_kwargs)
    assert cfg.daily_loss_limit_equity_pct == 0.0
    assert cfg.max_drawdown_equity_pct == 0.0


def test_default_rows_keep_the_deployed_guards(vt):
    spec = vt._spec(0.0, ("2020-01-01", "2020-02-01"), 100.0)
    assert "max_drawdown_equity_pct" not in spec.config_kwargs or (
        spec.config_kwargs["max_drawdown_equity_pct"] > 0
    )


def test_a_killed_run_is_flagged(vt):
    assert vt._killed({"exits": {"kill_switch": 3}}) == "yes"
    assert vt._killed({"entry_skips": {"killed": 12}}) == "yes"
    assert vt._killed({"exits": {"trailing_stop": 9}, "entry_skips": {}}) == "no"
