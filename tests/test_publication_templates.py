"""Publication templates use an explicit administrator placeholder."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_sudoers_templates_use_example_operator():
    for name in ("sudoers-ntx-deploy", "sudoers-ntx-ops"):
        text = (ROOT / "deploy" / name).read_text()
        assert "example-operator" in text
        assert "replace example-operator" in text


def test_readiness_check_allows_an_explicit_operator():
    text = (ROOT / "scripts/vps_check.sh").read_text()
    assert "${NTX_ADMIN_USER:-example-operator}" in text
