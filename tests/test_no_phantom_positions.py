"""F062: nautilus must not invent positions from fills it cannot explain.

On every restart the soak logged, five times in one afternoon:

    [ERROR] ExecEngine: Cannot open NETTING position ADA-USDT-SWAP.OKX-...
            from reduce-only fill TradeId('257396404')

and opened two phantom SHORT positions on XRP and DOGE. The cause is
`generate_missing_orders`, which nautilus documents as "If MARKET order events
will be generated during reconciliation to align discrepancies between internal
and external positions". A closing fill whose opening fill is outside the
reconciliation window looks exactly like such a discrepancy, so nautilus
fabricates the missing side.

Widening the window does not help: S003 holds a position for a median 28 hours,
so an opener can always fall outside any sane lookback.

The phantoms were harmless -- venue position reports square them, and the R44
drill proved none reaches the strategy's state -- but an ERROR that fires on
every restart is how an operator learns to ignore ERRORs, and the live node
will start producing them as soon as it has closed trades in the window.

This is safe for us specifically because nothing in this system trusts the
cache for safety decisions: adoption reads venue position reports (R27/R49),
the unprotected metric is defined over venue positions (R53), and the orphan
repair closes venue positions with no state (N2-04). The claim that adoption
still works with this off is not asserted here -- it is proven by running the
R44 drill against a real venue position.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from ntx.live import node

ROOT = Path(__file__).resolve().parents[1]


def test_reconciliation_does_not_fabricate_orders_unless_asked():
    """F062's lesson, now expressed as a default rather than a literal.

    nautilus will invent MARKET events for fills it cannot match, which produced
    phantom positions on every restart and, through them, the unintended live
    trades of F069. It is OFF unless something explicitly asks for it.

    It became a flag on 2026-10-06 so the demo bots can rehearse re-enabling it
    now that F071 widened the fill search from 60 minutes to 7 days -- the
    condition that made fabrication necessary in the first place. The money node
    inherits the safe value by doing nothing, and promotion is a deliberate edit
    to its unit file.
    """
    from ntx.config.settings import Settings

    assert Settings.model_fields["generate_missing_orders"].default is False
    live_unit = (ROOT / "deploy" / "ntx-s003.service").read_text()
    assert "NTX_GENERATE_MISSING_ORDERS=1" not in live_unit, (
        "live re-enables fabrication only after the demo rehearsal passes (R44)"
    )


def test_the_venue_is_still_reconciled_at_startup():
    """Turning off fabrication must not turn off reconciliation itself."""
    src = inspect.getsource(node.node_config)
    assert "reconciliation=True" in src
    assert "reconciliation_instrument_ids" in src


def test_position_and_open_order_checks_stay_on():
    """These are what keep the cache aligned with the venue once fabrication is
    off -- they poll the venue rather than guessing."""
    src = inspect.getsource(node.node_config)
    assert "position_check_interval_secs" in src
    assert "open_check_interval_secs" in src
