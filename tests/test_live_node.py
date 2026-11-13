"""Live node assembly -- tested entirely offline.

`plan()` and `node_config()` are pure, so every decision the node makes before
connecting is verified without a VPN or a venue.
"""

from __future__ import annotations

import pytest

from ntx.config.settings import Settings
from ntx.live import node


def _settings(tmp_path, **over) -> Settings:
    base = dict(
        NTX_VENUE="okx_live",
        NTX_TRADER_ID="NTX-TEST",
        OKX_API_KEY="k",
        OKX_API_SECRET="s",
        OKX_API_PASSPHRASE="p",
        OKX_DEMO_API_KEY="dk",
        OKX_DEMO_API_SECRET="ds",
        OKX_DEMO_API_PASSPHRASE="dp",
        OKX_BASE_URL_HTTP="https://www.okx.com",
        NTX_DATA_DIR=str(tmp_path),
    )
    base.update(over)
    return Settings(_env_file=None, **base)


# ------------------------------- host policy -------------------------------- #
@pytest.mark.parametrize(
    "host",
    [
        "https://okx.com",  # 301 redirect: POST (order placement) may not follow
        "https://my.okx.com",  # public data only: 401 code=50119 on private paths
        "https://app.okx.com",  # same
        "https://eea.okx.com",  # EEA entity routing for a GLOBAL account
        "http://www.okx.com",  # plaintext
    ],
)
def test_plan_rejects_hosts_that_cannot_serve_authenticated_calls(tmp_path, host):
    """F027: the user's .env pointed at bare okx.com -- a 301 that order
    placement would not survive. Measured, not assumed (docs/07)."""
    with pytest.raises(ValueError, match="authenticated"):
        node.plan(_settings(tmp_path, OKX_BASE_URL_HTTP=host))


def test_plan_accepts_the_canonical_host(tmp_path):
    p = node.plan(_settings(tmp_path))
    assert p.base_url_http == "https://www.okx.com"


def test_trailing_slash_is_tolerated(tmp_path):
    p = node.plan(_settings(tmp_path, OKX_BASE_URL_HTTP="https://www.okx.com/"))
    assert p.base_url_http == "https://www.okx.com"


# ------------------------------- plan rules --------------------------------- #
def test_plan_refuses_non_okx_venue(tmp_path):
    with pytest.raises(NotImplementedError, match="ADR-0010"):
        node.plan(_settings(tmp_path, NTX_VENUE="binance_demo"))


def test_plan_requires_live_credentials(tmp_path):
    with pytest.raises(RuntimeError, match="OKX_API_"):
        node.plan(_settings(tmp_path, OKX_API_SECRET=""))


def test_plan_requires_demo_credentials_in_demo(tmp_path):
    with pytest.raises(RuntimeError, match="OKX_DEMO_API_"):
        node.plan(_settings(tmp_path, NTX_VENUE="okx_demo", OKX_DEMO_API_KEY=""))


def test_demo_without_equity_override_is_refused(tmp_path):
    """Demo holds ~$95k of play money; sizing against it would make every
    rehearsal position 2500x the size live would take."""
    with pytest.raises(RuntimeError, match="NTX_EQUITY_OVERRIDE_USD"):
        node.plan(_settings(tmp_path, NTX_VENUE="okx_demo"))


def test_demo_uses_a_separate_journal(tmp_path):
    """Demo statistics must never pollute live ones."""
    live = node.plan(_settings(tmp_path))
    demo = node.plan(_settings(tmp_path, NTX_VENUE="okx_demo", NTX_EQUITY_OVERRIDE_USD="123.45"))
    assert live.journal_path.name == "journal.db"
    assert demo.journal_path.name == "journal-demo-S003.db"
    assert demo.demo and not live.demo


def test_journal_path_is_absolute(tmp_path):
    """R21: systemd's working directory is not the repo."""
    assert node.plan(_settings(tmp_path)).journal_path.is_absolute()


def test_blank_proxy_becomes_none(tmp_path):
    assert node.plan(_settings(tmp_path, OKX_PROXY_URL="")).proxy_url is None
    p = node.plan(_settings(tmp_path, OKX_PROXY_URL="socks5://127.0.0.1:1080"))
    assert p.proxy_url == "socks5://127.0.0.1:1080"


def test_default_universe_is_all_ten(tmp_path):
    p = node.plan(_settings(tmp_path))
    assert len(p.instrument_ids) == 10
    assert all(i.endswith(".OKX") for i in p.instrument_ids)


# ------------------------------ node config --------------------------------- #
def test_node_config_is_live_swap_linear_isolated(tmp_path):
    from nautilus_trader.core.nautilus_pyo3 import (
        OKXContractType,
        OKXEnvironment,
        OKXInstrumentType,
        OKXMarginMode,
    )

    s = _settings(tmp_path)
    cfg = node.node_config(node.plan(s), s)
    data = cfg.data_clients["OKX"]
    exe = cfg.exec_clients["OKX"]
    assert data.environment == OKXEnvironment.LIVE
    assert data.instrument_types == (OKXInstrumentType.SWAP,)
    assert data.contract_types == (OKXContractType.LINEAR,)
    assert exe.margin_mode == OKXMarginMode.ISOLATED
    assert data.base_url_http == exe.base_url_http == "https://www.okx.com"


def test_node_config_demo_environment(tmp_path):
    from nautilus_trader.core.nautilus_pyo3 import OKXEnvironment

    s = _settings(tmp_path, NTX_VENUE="okx_demo", NTX_EQUITY_OVERRIDE_USD="123.45")
    cfg = node.node_config(node.plan(s), s)
    assert cfg.data_clients["OKX"].environment == OKXEnvironment.DEMO
    assert cfg.data_clients["OKX"].api_key == "dk", "demo must use the demo key"


def test_node_config_reconciles_on_startup(tmp_path):
    """R27 / docs/08 §7: adopt venue truth before trading. This is what makes
    systemd Restart=always safe."""
    s = _settings(tmp_path)
    cfg = node.node_config(node.plan(s), s)
    assert cfg.exec_engine.reconciliation is True
    assert cfg.exec_engine.graceful_shutdown_on_exception is True


def test_node_config_loads_only_the_universe(tmp_path):
    s = _settings(tmp_path)
    cfg = node.node_config(node.plan(s), s)
    provider = cfg.data_clients["OKX"].instrument_provider
    assert provider.load_all is False
    assert len(provider.load_ids) == 10


def test_dry_run_never_connects(tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path)
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)

    def forbidden():
        raise AssertionError("dry run must not check egress or connect")

    monkeypatch.setattr("ntx.config.geo.check_egress", forbidden)
    assert node.main(["--dry-run"]) == 0
    assert "LIVE" in capsys.readouterr().out


def test_main_refuses_to_connect_from_a_restricted_country(tmp_path, monkeypatch, capsys):
    from ntx.config import geo

    s = _settings(tmp_path)
    monkeypatch.setattr("ntx.config.settings.load", lambda: s)
    monkeypatch.setattr("ntx.config.geo.check_egress", lambda: geo.assess("JP"))
    assert node.main([]) == 2
    assert "REFUSING" in capsys.readouterr().err


# ------------------------------- rehearsal ---------------------------------- #
def test_rehearsal_strategy_is_refused_on_live(tmp_path):
    """S900 enters unconditionally -- it must never reach real money."""
    with pytest.raises(RuntimeError, match="demo"):
        node.plan(_settings(tmp_path), strategy_id="S900")


def test_rehearsal_strategy_is_allowed_on_demo(tmp_path):
    p = node.plan(
        _settings(tmp_path, NTX_VENUE="okx_demo", NTX_EQUITY_OVERRIDE_USD="123.45"),
        strategy_id="S900",
    )
    assert p.demo and p.strategy_id == "S900"


def test_strategy_kwargs_set_live_only_flags(tmp_path):
    s = _settings(tmp_path, NTX_VENUE="okx_demo", NTX_EQUITY_OVERRIDE_USD="123.45")
    p = node.plan(s, strategy_id="S900", bar_spec="1-MINUTE-LAST-EXTERNAL")
    kw = node.strategy_kwargs(p, s)
    assert kw["is_demo"] is True
    assert kw["request_warmup"] is True
    assert kw["flatten_on_stop"] is True  # rehearsal leaves nothing open
    assert kw["bar_spec"] == "1-MINUTE-LAST-EXTERNAL"
    assert kw["equity_override_usd"] == 123.45


def test_production_strategy_does_not_flatten_on_stop(tmp_path):
    """A restart of S003 must leave positions and their venue stops alone."""
    s = _settings(tmp_path)
    kw = node.strategy_kwargs(node.plan(s), s)
    assert kw["flatten_on_stop"] is False
    assert kw["is_demo"] is False


def test_instrument_bases_resolve_to_nautilus_ids():
    assert node.resolve_instruments("sol,DOGE") == (
        node.BY_BASE["SOL"].nautilus_id,
        node.BY_BASE["DOGE"].nautilus_id,
    )
    with pytest.raises(KeyError):
        node.resolve_instruments("NOPE")


def test_client_order_ids_have_no_hyphens(tmp_path):
    """F035: OKX clOrdId must be alphanumeric; nautilus's default ids contain
    hyphens and every order was rejected with 'Parameter clOrdId error'."""
    s = _settings(tmp_path)
    kw = node.strategy_kwargs(node.plan(s), s)
    assert kw["use_hyphens_in_client_order_ids"] is False


def test_rehearsal_is_flattened_through_rest_after_the_node_stops(tmp_path):
    """F039: flatten_on_stop ran while the exec client was shutting down; its
    cancel/close tasks were aborted and rehearsal #5 left 2 demo positions."""
    s = _settings(tmp_path, NTX_VENUE="okx_demo", NTX_EQUITY_OVERRIDE_USD="123.45")
    assert node.needs_post_run_flatten(node.plan(s, strategy_id="S900")) is True
    live = _settings(tmp_path)
    assert node.needs_post_run_flatten(node.plan(live)) is False


def test_main_calls_the_post_run_flatten():
    import inspect

    assert "needs_post_run_flatten" in inspect.getsource(node.main)


# ---------------------------------------------------------------- F043 ---- #
def test_live_s003_uses_the_validated_parameters(tmp_path):
    """The soak node warmed up on 25 bars without complaint: it was running
    Donchian 20/10 (class defaults), not the validated 55/20."""
    from ntx.experiments.s003_gate import CONFIG as GATE_CONFIG

    s = _settings(tmp_path)
    kw = node.strategy_kwargs(node.plan(s), s)
    for k, v in GATE_CONFIG.items():
        assert kw[k] == v, f"{k}: live {kw[k]} != validated {v}"
    assert kw["entry_period"] == 55 and kw["exit_period"] == 20


def test_validated_config_pins_every_exit_parameter():
    """Class defaults may change; the deployed config must not drift with them."""
    from ntx.strategies.registry import DEPLOYED

    for key in ("sl_atr_mult", "tsl_enabled", "tsl_activation_price_pct", "tsl_trail_price_pct"):
        assert key in DEPLOYED["S003"]


def test_live_risk_settings_match_the_backtest_defaults(tmp_path):
    from ntx.strategies.base import NtxStrategyConfig

    s = _settings(tmp_path)
    kw = node.strategy_kwargs(node.plan(s), s)
    d = NtxStrategyConfig(instrument_ids=["X.OKX"])
    for k in ("risk_equity_pct", "max_leverage", "max_concurrent_positions"):
        assert kw[k] == getattr(d, k), k


# ---- OKX retires WebSocket port 8443 on 2026-10-31 (F086) ------------------ #


def _ws(cfg):
    return cfg.data_clients["OKX"].base_url_ws, cfg.exec_clients["OKX"].base_url_ws


def test_live_websockets_use_port_443_not_8443(tmp_path):
    """nautilus 1.230.0 defaults to wss://ws.okx.com:8443/...; OKX stops accepting
    8443 on 2026-10-31 (docs-v5 changelog, error 64010 in our logs since 10-25).
    Data = public URL, exec = private URL; business is derived from each."""
    s = _settings(tmp_path)
    data, execu = _ws(node.node_config(node.plan(s), s))
    assert data == "wss://ws.okx.com/ws/v5/public"
    assert execu == "wss://ws.okx.com/ws/v5/private"


def test_demo_websockets_use_port_443_not_8443(tmp_path):
    s = _settings(tmp_path, NTX_VENUE="okx_demo", NTX_EQUITY_OVERRIDE_USD="123.45")
    data, execu = _ws(node.node_config(node.plan(s), s))
    assert data == "wss://wspap.okx.com/ws/v5/public"
    assert execu == "wss://wspap.okx.com/ws/v5/private"
    assert ":8443" not in data + execu
