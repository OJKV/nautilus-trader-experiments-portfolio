"""Review finding: defaults pointed at S001, which is rejected and archived."""

from pathlib import Path

from ntx.config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_settings_default_strategy_is_the_mvp_one(monkeypatch):
    monkeypatch.delenv("NTX_STRATEGY", raising=False)
    assert Settings(_env_file=None).strategy == "S003"


def test_env_example_does_not_default_to_an_archived_strategy():
    lines = (ROOT / ".env.example").read_text().splitlines()
    assert "NTX_STRATEGY=S003" in lines
    assert "NTX_STRATEGY=S001" not in lines
