"""The random-entry control's published numbers must match the recorded run.

R42: every number in a doc needs a recorded run behind it. The S003 spec's §5
table did not have one. It reported a random maximum of 2.01 out-of-sample and
S003 at the 98th percentile (39/40); the only recorded control
(20260917T160318-random-entry-control-22e5eae5) has an OOS maximum of 1.52 and
S003 beating 40 of 40. `random_control.py`'s own docstring repeated the 2.01, and
built an argument on it -- "the distribution reaches Sharpe 2.01, which means a
lone backtest Sharpe between 1 and 2 is not evidence of edge".

That argument is still right; the number supporting it was invented. Both were
corrected against the run, and this test fails if either drifts again.

This is the control that decides whether S003's edge is the signal or the exits,
and the explainer is what the owner shows other people.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "tests/fixtures/random_control_manifest.json"


def _summary():
    return json.loads(RUN.read_text())["extra"]["summary"]


@pytest.mark.parametrize("window,want_max", [("IS", 0.485), ("OOS", 1.519)])
def test_the_recorded_maximum_is_what_we_publish(window, want_max):
    s = _summary()[window]
    assert max(s["random_sharpes"]) == pytest.approx(want_max, abs=0.001)


@pytest.mark.parametrize("window", ["IS", "OOS"])
def test_s003_beat_every_seed(window):
    """98th percentile (39/40) was published; the run says 40/40."""
    s = _summary()[window]
    beaten = sum(1 for x in s["random_sharpes"] if x < s["strategy_sharpe"])
    assert beaten == len(s["random_sharpes"]) == 40


def test_no_document_still_claims_a_random_maximum_of_2_01():
    """The specific invented figure, in every place it appeared."""
    for rel in (
        "docs/strategies/S003-donchian-breakout.md",
        "docs/strategies/S003-explained.md",
        "src/ntx/experiments/random_control.py",
    ):
        lines = (ROOT / rel).read_text().splitlines()
        for i, line in enumerate(lines):
            if "2.01" not in line:
                continue
            # Allowed only where the error itself is being described. A correction
            # is a sentence, not a line, so read the surrounding context.
            context = " ".join(lines[max(0, i - 2) : i + 3]).lower()
            assert any(
                w in context
                for w in ("wrong", "unsupported", "invented", "corrected", "none of", "no recorded")
            ), f"{rel}: unsupported 2.01 still presented as a result: {line.strip()}"
