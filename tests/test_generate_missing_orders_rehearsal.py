"""The reconstruct-missing-orders switch, behind a flag, demo first.

When the node restarts it asks OKX what it holds, then tries to rebuild how each
position came about by finding its opening fill. `generate_missing_orders`
decides what happens when it cannot find one: fabricate a plausible order, or
give up and leave the position unmanaged.

It was turned OFF 2026-09-2x (F062) for a real reason. The fill search was only
60 minutes wide, so the opener was *usually* missing, and nautilus invented one
on nearly every restart -- producing phantom SHORT positions. Those phantoms are
what caused the unintended live trades of 2026-09-28/29 (F069), twice.

F071 widened that search to 7 days, which removes the cause rather than the
symptom: with a window that actually spans a holding period, the real opener is
normally found and nothing needs inventing. That makes the switch worth
revisiting -- turning it back on is what lets a restart ADOPT a position like the
2026-10-05 BNB instead of abandoning it with no exits (F071).

"Probably safe now" is not evidence, and this flag has a real-money history, so
R44 applies: it goes on the demo bots, is watched, and reaches live only if they
adopt cleanly with no phantoms. The flag exists so that promotion is one line in
one unit file, not a code change under time pressure.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from ntx.config.settings import Settings
from ntx.live.node import node_config

ROOT = Path(__file__).resolve().parents[1]


def test_the_switch_is_off_unless_asked_for():
    """Default off: the money node inherits the safe setting by doing nothing."""
    assert Settings.model_fields["generate_missing_orders"].default is False


def test_the_node_reads_the_flag_rather_than_a_literal():
    src = inspect.getsource(node_config)
    assert "generate_missing_orders=False" not in src, "it must be configurable now"
    assert "generate_missing_orders" in src


def test_both_demo_units_turn_it_on_for_the_rehearsal():
    for unit in ("ntx-soak.service", "ntx-s004-demo2.service"):
        text = (ROOT / "deploy" / unit).read_text()
        assert "NTX_GENERATE_MISSING_ORDERS=1" in text, f"{unit} is not rehearsing it"


def test_the_live_unit_does_not():
    """Promotion is a deliberate edit to this file, after the demo evidence."""
    text = (ROOT / "deploy" / "ntx-s003.service").read_text()
    assert "NTX_GENERATE_MISSING_ORDERS=1" not in text, (
        "live gets it only after the demo rehearsal passes (R44)"
    )


def test_the_widened_lookback_is_still_in_place():
    """Re-enabling this without F071's 7-day window recreates F062 exactly."""
    src = inspect.getsource(node_config)
    assert "position_check_lookback_mins" in src
