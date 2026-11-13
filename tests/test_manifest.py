"""Run-lineage tests.

R9 requires every result to carry config + code version + data version. These
tests check the manifest actually captures enough to reproduce and to diff two
runs -- because an unreproducible result is not a result, and a lineage system
that silently records nothing is worse than none.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from ntx.backtest import manifest


def test_source_hash_covers_every_module():
    h = manifest.source_hash()
    assert len(h["combined"]) == 16
    assert len(h["files"]) > 20
    assert any("strategies/donchian_breakout.py" in k for k in h["files"])
    assert any("risk/sizing.py" in k for k in h["files"])


def test_source_hash_is_deterministic():
    assert manifest.source_hash()["combined"] == manifest.source_hash()["combined"]


def test_source_hash_changes_when_a_module_changes(tmp_path, monkeypatch):
    """Two runs with different code must be distinguishable."""
    pkg = tmp_path / "src" / "ntx"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(manifest, "REPO_ROOT", tmp_path)
    first = manifest.source_hash()
    (pkg / "a.py").write_text("x = 2\n")
    second = manifest.source_hash()
    assert first["combined"] != second["combined"]
    assert first["files"]["src/ntx/a.py"] != second["files"]["src/ntx/a.py"]


def test_git_sha_is_not_the_literal_string_head():
    """`git rev-parse HEAD` echoes 'HEAD' when a repo has no commits, which would
    otherwise be recorded as if it were a real revision."""
    sha = manifest.environment()["git_sha"]
    assert sha != "HEAD"
    assert sha == "NO_COMMITS" or (len(sha) == 40 and all(c in "0123456789abcdef" for c in sha))


def test_environment_pins_the_versions_that_change_results():
    env = manifest.environment()
    assert env["nautilus_trader"].startswith("1.")
    assert env["python"].startswith("3.")
    assert "git_dirty" in env


def test_data_fingerprint_absent_catalog(tmp_path):
    assert manifest.data_fingerprint(tmp_path) == {"present": False}


def test_data_fingerprint_detects_content_change(tmp_path):
    cat = tmp_path / "catalog" / "data" / "bar" / "X"
    cat.mkdir(parents=True)
    (cat / "a.parquet").write_bytes(b"0" * 100)
    first = manifest.data_fingerprint(tmp_path)
    (cat / "a.parquet").write_bytes(b"0" * 200)  # same name, different size
    second = manifest.data_fingerprint(tmp_path)
    assert first["present"] and first["files"] == 1
    assert first["hash"] != second["hash"], "a changed dataset must change the hash"


def test_record_writes_a_self_contained_run(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "RUNS_DIR", tmp_path / "runs")
    positions = pd.DataFrame({"realized_pnl": ["1.0 USDT"], "commissions": ["[0.1 USDT]"]})
    equity = pd.Series(
        [100.0, 110.0], index=pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC")
    )
    out = manifest.record(
        label="unit test/slash",
        spec={"a": 1},
        metrics={"sharpe": 1.5},
        positions=positions,
        equity=equity,
        extra={"script": "t"},
        stdout="hello",
    )
    assert (out / "manifest.json").exists()
    assert (out / "metrics.json").exists()
    assert (out / "trades.csv").exists()
    assert (out / "equity.csv").exists()
    assert (out / "stdout.txt").read_text() == "hello"

    m = json.loads((out / "manifest.json").read_text())
    for key in ("label", "spec", "environment", "source_hash", "data", "extra", "reproduce"):
        assert key in m, f"manifest missing {key}"
    assert "/" not in out.name, "label must be sanitised into the directory name"


def test_record_serialises_classes_and_dataclasses(tmp_path, monkeypatch):
    """Specs hold strategy CLASSES; json must not choke on them."""
    from ntx.backtest.runner import BacktestSpec
    from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy

    monkeypatch.setattr(manifest, "RUNS_DIR", tmp_path / "runs")
    spec = BacktestSpec(
        strategy_cls=DonchianBreakoutStrategy,
        config_cls=DonchianBreakoutConfig,
        config_kwargs=dict(entry_period=55),
    )
    out = manifest.record(label="spec", spec=spec)
    m = json.loads((out / "manifest.json").read_text())
    assert "DonchianBreakoutStrategy" in m["spec"]["strategy_cls"]
    assert m["spec"]["config_kwargs"]["entry_period"] == 55


def test_index_lists_runs_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "RUNS_DIR", tmp_path / "runs")
    for label, sharpe in (("first", 0.5), ("second", 1.5)):
        manifest.record(label=label, spec={}, metrics={"sharpe": sharpe, "trades": 10})
    df = manifest.index()
    assert len(df) == 2
    assert set(df["label"]) == {"first", "second"}
    assert {"source", "data", "nautilus", "sharpe"} <= set(df.columns)


def test_index_empty_when_nothing_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "RUNS_DIR", tmp_path / "nothing")
    assert manifest.index().empty


@pytest.mark.parametrize("label", ["a/b", "a b", "../escape", "x" * 200])
def test_labels_cannot_escape_the_runs_directory(tmp_path, monkeypatch, label):
    monkeypatch.setattr(manifest, "RUNS_DIR", tmp_path / "runs")
    out = manifest.record(label=label, spec={})
    assert (tmp_path / "runs") in out.parents
    assert len(out.name) < 120
