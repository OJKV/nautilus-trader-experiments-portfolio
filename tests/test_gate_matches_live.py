"""The account-specific gate settings must be explicitly provided locally."""

import importlib
import json
import sys

import pytest

from ntx.experiments import s003_gate as gate
from ntx.strategies.registry import DEPLOYED


def test_missing_private_configuration_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        gate.load_live_config(tmp_path / "missing.json")


def test_live_gate_requires_private_configuration_before_backtest(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "PRIVATE_CONFIG", tmp_path / "missing.json")
    with pytest.raises(FileNotFoundError):
        gate.main(["--live-config"])


def test_local_configuration_is_read_for_live_gate(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"equity_usd": 1234.0, "assets": ["BTC", "ETH"]}))
    assert gate.load_live_config(p) == (1234.0, ("BTC", "ETH"))


def test_invalid_configuration_is_rejected(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"equity_usd": -1, "assets": []}))
    with pytest.raises(ValueError):
        gate.load_live_config(p)


def test_gate_uses_deployed_strategy_parameters():
    assert gate.CONFIG is DEPLOYED["S003"]
    assert gate.LIVE_SLIPPAGE_BPS >= 5.0


@pytest.mark.parametrize(
    "name",
    [
        "donchian_fast_study",
        "cross_sectional_momentum_study",
        "volatility_breakout_study",
        "mean_reversion_study",
        "latency_stress",
        "venue_trail",
        "s004_gate",
    ],
)
def test_study_import_fails_before_work_when_private_config_missing(tmp_path, monkeypatch, name):
    monkeypatch.setattr(gate, "PRIVATE_CONFIG", tmp_path / "missing.json")
    module = f"ntx.experiments.{name}"
    monkeypatch.delitem(sys.modules, module, raising=False)
    with pytest.raises(FileNotFoundError):
        importlib.import_module(module)
