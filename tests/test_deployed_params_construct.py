"""Every DEPLOYED entry must actually build the config the node builds.

Measured 2026-09-25 11:15 UTC, on the first start of the S004 demo bot: the node
passed every assertion -- account, funding, leverage, preflight -- and then died
with

    TypeError: dict() got multiple values for keyword argument 'bar_spec'

because DEPLOYED["S004"] carried `bar_spec`, which `node_config` already supplies
from the plan. 947 unit tests passed, the box suite passed, and nothing noticed:
DEPLOYED is a plain dict that no test ever fed to a config class.

That is the same shape as F043 -- the node once ran class defaults because the
pinned parameters were never checked against what the node actually does with
them. A registry entry is code, and it needs a test that executes it.
"""

from __future__ import annotations

import pytest

from ntx.live.node import GATED_KEYS
from ntx.strategies.registry import DEPLOYED, STRATEGIES, get


@pytest.mark.parametrize("sid", sorted(DEPLOYED))
def test_the_pinned_parameters_build_the_strategy_config(sid):
    """Construct it exactly as `node_config` does, splat and all."""
    _cls, cfg_cls = get(sid)
    pinned = DEPLOYED[sid]
    kw = dict(
        **{k: v for k, v in pinned.items() if k not in GATED_KEYS},
        instrument_ids=["BTC-USDT-SWAP.OKX"],
        bar_spec="4-HOUR-LAST-EXTERNAL",
        journal_path="/tmp/x.db",
        equity_override_usd=1000.0,
        risk_equity_pct=pinned.get("risk_equity_pct", 0.5),
        max_leverage=pinned.get("max_leverage", 3.0),
        max_concurrent_positions=pinned.get("max_concurrent_positions", 3),
        is_demo=True,
    )
    cfg = cfg_cls(**kw)
    for key, want in pinned.items():
        assert getattr(cfg, key) == want, f"{sid}.{key} did not survive construction"


@pytest.mark.parametrize("sid", sorted(DEPLOYED))
def test_no_pinned_key_collides_with_what_node_assembly_supplies(sid):
    """These come from the plan or the environment, never from the registry."""
    supplied = {
        "instrument_ids",
        "bar_spec",
        "journal_path",
        "equity_override_usd",
        "is_demo",
        "request_warmup",
        "external_order_claims",
        "state_path",
        "daily_loss_limit_equity_pct",
        "max_drawdown_equity_pct",
    }
    clash = supplied & set(DEPLOYED[sid])
    assert not clash, f"DEPLOYED[{sid!r}] pins {sorted(clash)}, which node_config also passes"


@pytest.mark.parametrize("sid", sorted(DEPLOYED))
def test_every_pinned_key_is_a_real_config_field(sid):
    """A typo in the registry would otherwise be silently ignored."""
    import msgspec

    _cls, cfg_cls = get(sid)
    fields = {f.name for f in msgspec.structs.fields(cfg_cls)}
    unknown = set(DEPLOYED[sid]) - fields
    assert not unknown, f"DEPLOYED[{sid!r}] has unknown fields {sorted(unknown)}"


def test_every_deployed_strategy_is_registered():
    assert set(DEPLOYED) <= set(STRATEGIES)
