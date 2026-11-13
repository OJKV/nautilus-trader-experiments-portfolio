"""S004 runs on demo to exercise the execution path. It must never reach money.

It FAILED its out-of-sample gate: PF after funding 1.07 against 1.15 required,
fee drag 64% of gross, breadth 4/10 instruments positive against the 6 docs/09
requires, and the top 10 trades were 326% of total return -- the rest of the book
is net negative. Its in-sample winner is a single cell whose four neighbours all
fail PF and all latch the drawdown KILL (F024's isolated-peak shape).

It is deployed anyway, on its own demo sub-account, for one reason: it trades
about 1.36 times a day against S003's 0.83, so entries, venue stops, fills,
adoption and the watchdog get exercised far more often. Every real defect found
this week came from the execution path, not the signal.

So the guard cannot be a comment. `REHEARSAL_ONLY` is the wrong home -- that set
also means "flatten everything on stop" (S900's semantics), which would close
S004's positions on every deploy and destroy the very execution history it exists
to produce. Hence a separate set.
"""

from __future__ import annotations

import pytest

from ntx.config.settings import Settings
from ntx.strategies import registry


def _settings(venue: str) -> Settings:
    return Settings(
        _env_file=None,
        NTX_VENUE=venue,
        OKX_DEMO_API_KEY="k",
        OKX_DEMO_API_SECRET="s",
        OKX_DEMO_API_PASSPHRASE="p",
        OKX_API_KEY="k",
        OKX_API_SECRET="s",
        OKX_API_PASSPHRASE="p",
        NTX_EQUITY_OVERRIDE_USD="1000",
    )


def test_s004_is_registered():
    assert "S004" in registry.STRATEGIES
    cls, _cfg = registry.get("S004")
    assert cls.__name__ == "CrossSectionalMomentumStrategy"


def test_s004_is_marked_demo_only():
    assert "S004" in registry.DEMO_ONLY


def test_the_live_node_refuses_s004():
    """The enforcement, not the intention."""
    from ntx.live.node import plan

    with pytest.raises(RuntimeError, match="DEMO ONLY"):
        plan(_settings("okx_live"), strategy_id="S004")


def test_the_demo_node_accepts_s004():
    from ntx.live.node import plan

    p = plan(_settings("okx_demo"), strategy_id="S004")
    assert p.strategy_id == "S004"


def test_s004_does_not_flatten_on_stop():
    """REHEARSAL_ONLY means 'flatten everything on stop'. S004 must hold its
    positions across a deploy, or the execution history it exists to generate is
    destroyed every time code ships."""
    assert "S004" not in registry.REHEARSAL_ONLY


def test_s004_deployed_params_are_the_ones_that_were_measured():
    """R47: what runs is what was validated -- even when what was validated
    failed, because the run has to stay reproducible."""
    d = registry.DEPLOYED["S004"]
    assert d["lookback"] == 90
    assert d["rebalance_bars"] == 6
    assert d["long_n"] == 3
    assert d["short_n"] == 0
    assert d["reversal"] is False
    for key in ("risk_equity_pct", "max_leverage", "max_concurrent_positions"):
        assert key in d, f"{key} unpinned: .env would decide what runs (R47/F043)"


def test_the_registry_records_that_it_failed():
    """Nobody should have to read a commit message to learn this."""
    import inspect

    src = inspect.getsource(registry)
    assert "DEMO_ONLY" in src
    low = src.lower()
    assert "failed" in low and "out-of-sample" in low


def test_the_unit_points_at_the_second_sub_account():
    """The whole reason this bot exists on its own account (F046/R51/F064)."""
    from pathlib import Path

    unit = Path("deploy/ntx-s004-demo2.service").read_text()
    assert "NTX_KEYSET=demo2" in unit
    assert "NTX_VENUE=okx_demo" in unit
    assert "--strategy S004" in unit


def test_the_unit_says_what_it_is():
    """An operator reading `systemctl status` must not mistake this for a
    strategy anyone believes in."""
    from pathlib import Path

    unit = Path("deploy/ntx-s004-demo2.service").read_text()
    assert "NOT promotable" in unit
    assert "FAILED" in unit


def test_it_restarts_like_a_real_bot_and_stays_down_on_a_refusal():
    from pathlib import Path

    unit = Path("deploy/ntx-s004-demo2.service").read_text()
    assert "Restart=always" in unit
    assert "RestartPreventExitStatus=2" in unit
    assert "KillSignal=SIGTERM" in unit
    assert "OnFailure=" in unit
