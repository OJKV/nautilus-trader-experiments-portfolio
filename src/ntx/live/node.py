"""Live / paper TradingNode assembly.

Everything here that can be decided without a network connection is a pure
function returning configuration, so it is unit-tested offline. Only `main()`
connects -- and it refuses to do so unless the egress country is allowed
(`config.geo`) and the preflight gate passes.

One node, one process (nautilus requirement, docs/13 §2).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from nautilus_trader.adapters.okx.config import OKXDataClientConfig, OKXExecClientConfig
from nautilus_trader.config import (
    CacheConfig,
    InstrumentProviderConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.core.nautilus_pyo3 import (
    OKXContractType,
    OKXEnvironment,
    OKXInstrumentType,
    OKXMarginMode,
)
from nautilus_trader.model.identifiers import InstrumentId

from ntx.config.geo import check_egress
from ntx.config.settings import REPO_ROOT, Settings
from ntx.config.universe import BY_BASE, UNIVERSE
from ntx.config.venues import OKX

VENUE_NAME = "OKX"

# The ONLY host measured to serve /api/v5/account/* for this account (docs/07).
# Everything else fails for a specific, verified reason:
#   okx.com      301 redirect -- POST (order placement) may not follow it
#   my./app.     CDN front doors: 401 code=50119 on private paths
#   eea.         EEA entity routing for a GLOBAL-entity account
AUTH_HOST = "https://www.okx.com"


def _validate_auth_host(url: str) -> str:
    cleaned = (url or "").strip().rstrip("/")
    if cleaned != AUTH_HOST:
        raise ValueError(
            f"OKX_BASE_URL_HTTP={url!r} cannot serve authenticated calls; "
            f"use {AUTH_HOST} (see docs/07-networking-and-geo.md)"
        )
    return cleaned


@dataclass(frozen=True)
class NodePlan:
    """Everything decided before connecting -- inspectable and testable."""

    trader_id: str
    demo: bool
    base_url_http: str
    proxy_url: str | None
    instrument_ids: tuple[str, ...]
    strategy_id: str
    bar_spec: str
    journal_path: Path
    equity_override_usd: float | None


def plan(
    settings: Settings,
    *,
    strategy_id: str = "S003",
    bar_spec: str = "4-HOUR-LAST-EXTERNAL",
    instrument_ids: tuple[str, ...] | None = None,
) -> NodePlan:
    """Decide the node's shape from settings. Pure; no network."""
    if not settings.venue.startswith("okx"):
        raise NotImplementedError(f"NTX_VENUE={settings.venue!r}: MVP is OKX-only (ADR-0010)")
    demo = settings.is_demo
    from ntx.strategies.registry import DEMO_ONLY, REHEARSAL_ONLY

    if strategy_id in REHEARSAL_ONLY and not demo:
        raise RuntimeError(
            f"{strategy_id} is a demo-only rehearsal (it enters unconditionally); "
            f"refusing with NTX_VENUE={settings.venue!r}"
        )
    if strategy_id in DEMO_ONLY and not demo:
        # A strategy that failed its gate is registered so its runs stay
        # reproducible and so it can exercise the execution path on demo. The
        # refusal is here rather than in a comment because the whole point is
        # that nobody can promote it by forgetting.
        raise RuntimeError(
            f"{strategy_id} is DEMO ONLY: it did not pass the out-of-sample gate "
            f"(see registry.DEMO_ONLY). Refusing with NTX_VENUE={settings.venue!r}. "
            f"Promotion means re-running the gate, not editing the registry."
        )
    # The strategy id makes creds() refuse a live strategy on another's key (E3-03).
    key, secret, passphrase = settings.creds(strategy_id)
    if not all((key, secret, passphrase)):
        which = "OKX_DEMO_API_*" if demo else "OKX_API_*"
        raise RuntimeError(f"missing credentials: set {which} in .env")
    if not demo and settings.equity_override_usd is not None:
        # A frozen equity means size never shrinks as the account loses (R5) and
        # the loss guards see equity == high-water == day-start, so neither can
        # ever fire. .env.example once shipped it next to NTX_VENUE=okx_live.
        raise RuntimeError(
            "NTX_EQUITY_OVERRIDE_USD is set on a LIVE account: it freezes position "
            "size and disables both loss guards -- remove the line from .env"
        )
    if demo and settings.equity_override_usd is None:
        # Demo holds ~$95k; without an override every size is 2500x live.
        raise RuntimeError(
            "demo session without NTX_EQUITY_OVERRIDE_USD would size positions "
            "against ~$95k of play money -- set it to the live equity"
        )
    return NodePlan(
        # One trader id per strategy: two nodes on one account must never
        # generate the same clOrdId (ADR-0014).
        trader_id=f"NTX-{strategy_id[1:]}" if strategy_id[1:].isdigit() else settings.trader_id,
        demo=demo,
        base_url_http=_validate_auth_host(settings.okx_base_url_http or OKX.auth_base_url),
        proxy_url=settings.okx_proxy_url or None,
        instrument_ids=instrument_ids or tuple(a.nautilus_id for a in UNIVERSE),
        strategy_id=strategy_id,
        bar_spec=bar_spec,
        journal_path=(
            settings.data_dir
            / (f"journal-demo-{strategy_id}.db" if demo else _live_journal_name(strategy_id))
        ).resolve(),
        equity_override_usd=settings.equity_override_usd,
    )


def _live_journal_name(strategy_id: str) -> str:
    # S003 keeps the journal it already has (R29); later strategies get their own.
    return "journal.db" if strategy_id == "S003" else f"journal-{strategy_id}.db"


def usdt_equity(balance: dict) -> float:
    """USDT equity from /account/balance -- the same basis the strategy sizes on (F045).
    `totalEq` would add coin dust the strategy never sees."""
    for d in balance["data"][0].get("details", []):
        if d.get("ccy") == "USDT":
            return float(d.get("eq") or 0.0)
    return 0.0


def apply_preflight(p: NodePlan, keep: list[str], held: tuple[str, ...] = ()) -> NodePlan:
    """Narrow the plan to instruments the account can actually trade (G5).

    An instrument that currently HOLDS a position is always kept, however the
    minimums look now: dropping it means it is not subscribed, not reconciled
    and not adopted, so its position is left unmanaged with only the venue stop
    (L-05). For the same reason "nothing tradable" is not fatal while positions
    are open -- the node starts to manage what it holds.
    """
    from dataclasses import replace

    from ntx.preflight import NoTradableInstruments

    allowed = set(keep) | set(held)
    kept = tuple(i for i in p.instrument_ids if i in allowed)
    if not kept:
        raise NoTradableInstruments(f"no tradable instrument among {p.instrument_ids}")
    return replace(p, instrument_ids=kept)


def held_instruments(p: NodePlan, settings: Settings) -> tuple[str, ...]:
    """Instrument ids with an open position at the venue (R27: venue is truth)."""
    from ntx.doctor import _signed_get

    try:
        r = _signed_get(p.base_url_http, "/api/v5/account/positions", settings.creds(), p.demo)
    except Exception as e:  # never block a start on this lookup
        print(f"preflight: cannot read positions ({e})", flush=True)
        return ()
    if r.get("code") != "0":
        print(f"preflight: cannot read positions ({r.get('code')} {r.get('msg')})", flush=True)
        return ()
    out = []
    for row in r.get("data") or []:
        try:
            if float(row.get("pos") or 0) != 0:
                out.append(f"{row['instId']}.OKX")
        except (TypeError, ValueError):
            continue
    return tuple(out)


def parse_tickers(data: list[dict], wanted: set[str]) -> tuple[dict[str, float], list[str]]:
    """Last prices for OUR instruments; never raise on someone else's (F084).

    OKX listed TEST002/TEST003-USDT-SWAP with `last: ""` on 2026-10-24, and a
    `float()` over the whole swap universe kept all three bots from starting for
    38 h (R18, R73). Returns (prices, our instruments with no usable price).
    """
    prices: dict[str, float] = {}
    for t in data:
        inst = f"{t.get('instId', '')}.OKX"
        if inst not in wanted:
            continue
        try:
            px = float(t.get("last") or "nan")
        except (TypeError, ValueError):
            continue
        if px > 0:
            prices[inst] = px
    return prices, sorted(wanted - set(prices))


def run_preflight(p: NodePlan, settings: Settings) -> tuple[NodePlan, float]:
    """Fetch specs, prices and equity; drop instruments below venue minimums.

    Live equity is read from the venue here -- the same number the strategy
    will size from -- so a wrong balance shows up before anything trades (G6).
    """
    import json
    import urllib.request

    from ntx.config.venues import BROWSER_UA
    from ntx.data.instruments import build_all
    from ntx.doctor import _signed_get
    from ntx.preflight import report, tradable_universe
    from ntx.risk.sizing import SizingLimits

    instruments = {
        str(i.id): i
        for i in build_all(
            settings.data_dir / "okx_specs_live.json", p.base_url_http, refresh=True
        ).values()
        if str(i.id) in p.instrument_ids
    }
    req = urllib.request.Request(
        f"{p.base_url_http}/api/v5/market/tickers?instType=SWAP",
        headers={"User-Agent": BROWSER_UA},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        tickers, unpriced = parse_tickers(json.loads(r.read())["data"], set(p.instrument_ids))
    if unpriced:
        # Kept in the plan: held positions stay managed (held_instruments below),
        # only new entries wait for a price. Dropping them was F071's door.
        print(f"preflight: WARNING no usable price for {', '.join(unpriced)}", flush=True)
    if p.equity_override_usd is not None:
        equity = float(p.equity_override_usd)
    else:
        bal = _signed_get(p.base_url_http, "/api/v5/account/balance", settings.creds(), p.demo)
        if bal.get("code") != "0":
            raise Unavailable(f"cannot read live balance: {bal.get('code')} {bal.get('msg')}")
        equity = usdt_equity(bal)
    limits = SizingLimits(
        risk_equity_pct=settings.risk_equity_pct,
        max_leverage=settings.max_leverage,
        max_concurrent_positions=settings.max_concurrent_positions,
    )
    keep, results = tradable_universe(
        instruments=instruments, prices=tickers, equity_usd=equity, limits=limits
    )
    print(report(results, equity), flush=True)
    held = held_instruments(p, settings)
    if held:
        print(f"preflight: keeping held instruments {', '.join(held)}", flush=True)
    return apply_preflight(p, keep, held=held), equity


# acctLv 1 = Spot mode: margin swaps are not permitted and EVERY order is
# rejected with "You can't complete this request under your current account
# mode." 2 = single-currency margin (what we validated and what live uses),
# 3 = multi-currency, 4 = portfolio. Anything >= 2 can place our orders.
REQUIRED_ACCT_LV = 2


def account_mode_problem(config: dict) -> str | None:
    """Why this account cannot run our orders, or None if it can (F055).

    `doctor` used to print `acctLv` and compare nothing, so a demo account
    sitting in Spot mode looked healthy for three hours of soak while every
    entry was refused by the venue.
    """
    try:
        lv = int(str((config or {}).get("acctLv")))
    except (TypeError, ValueError):
        return f"cannot read acctLv from the account config: {config!r}"
    if lv < REQUIRED_ACCT_LV:
        return (
            f"acctLv={lv} (Spot mode): OKX refuses margined SWAP orders here. "
            f"Set the account to Single-currency margin (acctLv={REQUIRED_ACCT_LV}) "
            f"-- OKX web/app: Assets -> Account mode."
        )
    pos_mode = str((config or {}).get("posMode") or "")
    if pos_mode and pos_mode != "net_mode":
        return (
            f"posMode={pos_mode}: every order and the leverage call here assume "
            f"net_mode (they deliberately omit posSide)"
        )
    return None


# Exit codes. Both units set RestartPreventExitStatus=2, so this distinction is
# the difference between "systemd leaves it down" and "systemd tries again":
#
#   0  clean stop            2  deterministic refusal -- the answer is the same in 120 s
#   1  internal error        4  could not determine -- ask again (L2-03)
#
# A refusal is an answer we do not like (Spot mode, leverage drift, not funded,
# not armed, a second instance, a restricted country). Unavailable is the absence
# of an answer: a timeout, a rate limit, a call that left over IPv6 (F061). Those
# used to share exit 2, so one 10 s venue hiccup at start-up left a live position
# with its venue stop but no Donchian exit, no trailing stop, no watchdog and no
# loss guards until a human noticed.
EXIT_REFUSED = 2
EXIT_UNAVAILABLE = 4


class Unavailable(RuntimeError):
    """A venue read did not complete. It says nothing about whether we may trade.

    Subclasses RuntimeError so every existing `except RuntimeError` -- notably
    observe mode, which reports and continues -- keeps working unchanged.
    """


def resolve_egress(*, attempts: int = 3, check=None, sleep=None):
    """The egress verdict, retried while the lookup itself keeps failing.

    `check_egress` fails closed with `country=None` when it could not reach the
    trace endpoint at all (geo.py:183-186). That is not "you are in Japan"; a VPS
    that has just rebooted may simply not have DNS yet. A determined verdict --
    allowed or restricted -- is returned immediately and never retried, because
    retrying a deterministic answer only delays it.
    """
    import time

    check = check or check_egress
    sleep = sleep or time.sleep
    v = check()
    for i in range(1, attempts):
        if v.country is not None:
            return v
        sleep(min(2**i, 8))
        v = check()
    return v


def fetch_account_config(p: NodePlan, settings: Settings) -> dict:
    from ntx.ops.okx_rest import signed

    body = signed("GET", p.base_url_http, "/api/v5/account/config", settings.creds(), p.demo)
    if str(body.get("code")) != "0":
        raise Unavailable(f"cannot read the account config: {body.get('code')} {body.get('msg')}")
    return (body.get("data") or [{}])[0]


def describe_account(config: dict) -> str:
    """Which account a key belongs to, in words (F059).

    The OKX app shows the MAIN account by default; the bot's keys may belong to
    a sub-account (ADR-0014). Without saying so, "$106K in the app, $0 in the
    bot" looks like something broke.
    """
    uid, main = str(config.get("uid") or ""), str(config.get("mainUid") or "")
    label = config.get("label") or "?"
    if uid and main and uid != main:
        return f"sub-account '{label}'"
    return f"main account (key '{label}')"


def assert_account_mode(p: NodePlan, settings: Settings) -> str:
    """Refuse to start an account that cannot place our orders at all.

    Returns the account description, for the snapshot and the monitor.
    """
    config = fetch_account_config(p, settings)
    problem = account_mode_problem(config)
    who = describe_account(config)
    if problem:
        raise RuntimeError(f"{'demo' if p.demo else 'LIVE'} {who}: {problem}")
    print(
        f"account: {'demo' if p.demo else 'LIVE'} {who}; acctLv>={REQUIRED_ACCT_LV}, net_mode "
        f"-- can place our orders",
        flush=True,
    )
    return who


GATED_KEYS = (
    "risk_equity_pct",
    "max_leverage",
    "max_concurrent_positions",
    # The loss limits are the kill switch's thresholds (E3-04). Compared only
    # once registry.DEPLOYED pins them, like every key here.
    "daily_loss_limit_equity_pct",
    "max_drawdown_equity_pct",
)


def assert_matches_gate(p: NodePlan, settings: Settings) -> None:
    """Refuse a LIVE start whose .env disagrees with what was validated (R47).

    The node runs the pinned values either way; on live a disagreement means the
    operator believes one thing and the account does another, so it is a refusal
    rather than a silent substitution. Demo just warns.
    """
    from ntx.strategies.registry import DEPLOYED

    pinned = DEPLOYED.get(p.strategy_id, {})
    diffs = [
        f"{k}: .env={getattr(settings, k)} gated={pinned[k]}"
        for k in GATED_KEYS
        if k in pinned and float(getattr(settings, k)) != float(pinned[k])
    ]
    if not diffs:
        return
    msg = "; ".join(diffs)
    if p.demo:
        print(
            f"WARNING: .env disagrees with the gate, running the gated values ({msg})", flush=True
        )
        return
    raise RuntimeError(f".env disagrees with what was validated -- fix .env ({msg})")


# The smallest account worth trading at all: below this even a minimum-size
# order cannot be margined.
MIN_FUNDED_USD = 5.0


def fetch_usdt_equity(p: NodePlan, settings: Settings) -> float:
    """What the account really holds in USDT, ignoring any sizing override."""
    from ntx.ops.okx_rest import signed

    body = signed("GET", p.base_url_http, "/api/v5/account/balance", settings.creds(), p.demo)
    if str(body.get("code")) != "0":
        raise Unavailable(f"cannot read the balance: {body.get('code')} {body.get('msg')}")
    return usdt_equity(body)


def assert_funded(p: NodePlan, settings: Settings) -> None:
    """Refuse to start an account that cannot fund the orders it will size (F059).

    The demo soak sizes from NTX_EQUITY_OVERRIDE_USD so its trades match the
    real account -- and that override hid an EMPTY demo account for a day: the
    first breakout came back `Insufficient USDT balance` on five instruments.
    An override may shrink sizing to the real account; it must never stand in
    for money that is not there.
    """
    held = fetch_usdt_equity(p, settings)
    sized_as = settings.equity_override_usd
    where = "demo" if p.demo else "LIVE"
    if held < MIN_FUNDED_USD:
        raise RuntimeError(
            f"the {where} account holds ${held:,.2f} of USDT -- not funded. "
            + (
                "Top up demo funds in the OKX app (Demo trading -> Assets)."
                if p.demo
                else "Deposit USDT to the trading account."
            )
        )
    if sized_as is not None and float(sized_as) > held:
        raise RuntimeError(
            f"sizing as if ${float(sized_as):,.2f} but the {where} account holds only "
            f"${held:,.2f}: orders would be refused for insufficient balance"
        )
    print(f"funded: {where} account holds ${held:,.2f} of USDT", flush=True)


def assert_leverage(p: NodePlan, settings: Settings) -> None:
    """Refuse to trade an instrument whose VENUE leverage is not what we sized for.

    `NTX_MAX_LEVERAGE` is a local sizing ceiling; OKX keeps its own number per
    instrument, and in isolated margin that number sets the liquidation price.
    At 3x liquidation is ~33% away and our 2xATR stop is 4-8% -- fine. At OKX's
    higher defaults (10x -> ~9.6%) liquidation moves inside the tail of our own
    stop, so the exchange would close the trade before the stop fired (L-07).

    Live: refuse to start. Demo: warn -- a demo account cannot lose money, and
    blocking the soak over it would cost more than it saves.
    """
    from ntx.ops.leverage import drift, read_leverage

    target = settings.max_leverage
    try:
        configured = read_leverage(list(p.instrument_ids), settings, p.demo, base=p.base_url_http)
    except Exception as e:
        if p.demo:
            print(f"leverage check skipped: {e}", flush=True)
            return
        raise Unavailable(f"cannot verify venue leverage before trading live: {e}") from e
    bad = drift(configured, target, list(p.instrument_ids))
    if not bad:
        print(f"leverage: every instrument is at {target:g}x isolated", flush=True)
        return
    detail = ", ".join(f"{k}={v if v is not None else '?'}" for k, v in sorted(bad.items()))
    fix = f"python -m ntx.ops.leverage --set --{'demo' if p.demo else 'live'}"
    if p.demo:
        print(f"WARNING: venue leverage is not {target:g}x for {detail} -- fix with: {fix}", flush=True)  # fmt: skip
        return
    raise RuntimeError(
        f"venue leverage is not {target:g}x for {detail}. At a higher leverage the "
        f"liquidation price can sit inside our own stop. Fix with: {fix}"
    )


def needs_post_run_flatten(p: NodePlan) -> bool:
    """Rehearsals must leave the demo account flat. `flatten_on_stop` alone is not
    enough: it runs while the exec client is shutting down and its tasks get
    aborted (F039), so the node's exit is followed by a REST flatten."""
    from ntx.strategies.registry import REHEARSAL_ONLY

    return p.demo and p.strategy_id in REHEARSAL_ONLY


def resolve_instruments(bases: str) -> tuple[str, ...]:
    """'sol,DOGE' -> nautilus instrument ids. Unknown bases raise KeyError."""
    out = []
    for b in (x.strip().upper() for x in bases.split(",") if x.strip()):
        if b not in BY_BASE:
            raise KeyError(f"unknown asset {b!r}; known: {sorted(BY_BASE)}")
        out.append(BY_BASE[b].nautilus_id)
    return tuple(out)


def strategy_kwargs(p: NodePlan, settings: Settings, **extra) -> dict:
    """Strategy config for a live/demo node. Live-only flags are set HERE, in
    node assembly, never inside strategy code (R3)."""
    from ntx.strategies.registry import DEPLOYED, REHEARSAL_ONLY

    pinned = DEPLOYED.get(p.strategy_id, {})
    # The gated risk keys are set explicitly below (registry first, .env only as
    # a fallback), so they are left out of the splat to avoid a double keyword.
    kw = dict(
        **{k: v for k, v in pinned.items() if k not in GATED_KEYS},
        instrument_ids=list(p.instrument_ids),
        bar_spec=p.bar_spec,
        journal_path=str(p.journal_path),
        equity_override_usd=p.equity_override_usd,
        # What was gated wins over .env (R47); .env only fills what is not pinned.
        risk_equity_pct=pinned.get("risk_equity_pct", settings.risk_equity_pct),
        max_leverage=pinned.get("max_leverage", settings.max_leverage),
        max_concurrent_positions=pinned.get(
            "max_concurrent_positions", settings.max_concurrent_positions
        ),
        daily_loss_limit_equity_pct=settings.daily_loss_limit_equity_pct,
        max_drawdown_equity_pct=settings.max_drawdown_equity_pct,
        is_demo=p.demo,
        request_warmup=True,
        # Reconciled orders/positions on these instruments belong to this
        # strategy, not to 'EXTERNAL' (G1 restart adoption).
        external_order_claims=[InstrumentId.from_str(i) for i in p.instrument_ids],
        flatten_on_stop=p.strategy_id in REHEARSAL_ONLY,
        # OKX clOrdId must be alphanumeric, <= 32 chars (F035).
        use_hyphens_in_client_order_ids=False,
        # Snapshot for the read-only monitor (ntx.ui), written every 15 s.
        state_path=str(
            (
                settings.data_dir / "state" / f"{p.strategy_id}-{'demo' if p.demo else 'live'}.json"
            ).resolve()
        ),
        subscribe_quotes=True,
        # OKX rejects reduce-only stops without close_fraction (F036).
        venue_stop_closes_position=True,
        venue_attaches_stop=settings.attach_stop_at_entry,
    )
    kw.update(extra)
    return kw


# OKX stops accepting WebSocket connections on port 8443 from 2026-10-31 (docs-v5
# changelog; error 64010 "WebSocket port 8443 will be retired soon" in our logs
# since 2026-10-25). nautilus 1.230.0's defaults are wss://ws.okx.com:8443/...,
# so the URLs are set here, port-less (= 443), verified from the box: HTTP 101 on
# public/private/business for both hosts. The data client reads `base_url_ws` as
# the PUBLIC url and the exec client as the PRIVATE one; each derives its
# business channel from it (F086, R76).
OKX_WS_HOST = {False: "wss://ws.okx.com", True: "wss://wspap.okx.com"}


def node_config(p: NodePlan, settings: Settings) -> TradingNodeConfig:
    """Translate a plan into nautilus configuration. Pure; no network."""
    key, secret, passphrase = settings.creds()
    env = OKXEnvironment.DEMO if p.demo else OKXEnvironment.LIVE
    provider = InstrumentProviderConfig(load_all=False, load_ids=frozenset(p.instrument_ids))
    common = dict(
        api_key=key,
        api_secret=secret,
        api_passphrase=passphrase,
        instrument_types=(OKXInstrumentType.SWAP,),
        contract_types=(OKXContractType.LINEAR,),
        environment=env,
        base_url_http=p.base_url_http,
        proxy_url=p.proxy_url,
        instrument_provider=provider,
    )
    return TradingNodeConfig(
        trader_id=p.trader_id,
        # JSON lines on disk for the monitor and post-mortems (docs/11 §6).
        logging=LoggingConfig(
            log_level=settings.log_level,
            log_level_file="INFO",
            log_directory=str((REPO_ROOT / "logs").resolve()),
            log_file_name=f"ntx-{p.strategy_id}-{'demo' if p.demo else 'live'}",
            log_file_format="json",
            log_file_max_size=50 * 1024 * 1024,
            log_file_max_backup_count=14,
        ),
        cache=CacheConfig(),
        exec_engine=LiveExecEngineConfig(
            # Startup reconciliation: adopt venue truth before trading (R27).
            reconciliation=True,
            # Must span a whole hold, like the position check below (F080). At
            # 1,440 min the 2026-10-06 restarts saw ADA's closing sell (5 h old)
            # but not its opening buy (41 h old), and a lone sell in an empty
            # cache is a short: a phantom on every restart for a day. Holds over
            # seven days can still produce one; entries refuse that instrument
            # and every close asks the venue (F073/F074), so it costs trades,
            # never money.
            reconciliation_lookback_mins=10_080,
            # A DIFFERENT knob, and the one that mattered (F071). The query behind
            # a *position discrepancy* uses `position_check_lookback_mins`, which
            # defaults to 60 -- so on a strategy holding 4h-bar positions for days,
            # every restart more than an hour after the last fill left its open
            # positions unmanaged. Measured 2026-10-05: BNB, opened 43 hours
            # earlier, came back as "no missing fills found" and the node ran with
            # no state for a real live position. Its stop still capped the loss;
            # the Donchian exit and the trailing stop could not run, and on S003
            # the trailing stop is where the winners came from.
            #
            # Seven days covers the overwhelming majority of holds (the account was
            # measured flat only 31.5% of the time, worst case 28.5 days). It only
            # ever costs a wider fill query, and only when a discrepancy is found.
            position_check_lookback_mins=10_080,
            # Only our instruments: another node may trade others on this account.
            reconciliation_instrument_ids=[InstrumentId.from_str(i) for i in p.instrument_ids],
            # Do NOT let nautilus invent MARKET events to explain fills it cannot
            # match (F062). A closing fill whose opener is outside the lookback
            # looks like a discrepancy, so it fabricated the missing side and
            # opened phantom SHORT positions on every restart. Widening the window
            # does not help: a position is held for a median 28 hours, so the
            # opener can always fall outside it.
            #
            # Safe here because nothing trusts the cache for a safety decision:
            # adoption reads venue position reports (R27/R49), the unprotected
            # metric is defined over venue positions (R53), and the orphan repair
            # closes venue positions with no state (N2-04). Proven by the R44
            # drill, which asserts a real position is still adopted with this off.
            # Flagged, not literal (R44 rehearsal): default off, on for the demo
            # bots, promoted to live only once they have adopted cleanly. Re-enabling
            # this is only safe BECAUSE `position_check_lookback_mins` above now
            # spans a real holding period -- without that it recreates F062.
            generate_missing_orders=settings.generate_missing_orders,
            # Rust-level errors must stop the node, not leave it half-alive.
            graceful_shutdown_on_exception=True,
            inflight_check_interval_ms=2_000,
            open_check_interval_secs=10,
            position_check_interval_secs=60,
        ),
        data_clients={
            VENUE_NAME: OKXDataClientConfig(
                **common, base_url_ws=f"{OKX_WS_HOST[p.demo]}/ws/v5/public"
            )
        },
        exec_clients={
            VENUE_NAME: OKXExecClientConfig(
                **common,
                base_url_ws=f"{OKX_WS_HOST[p.demo]}/ws/v5/private",
                margin_mode=OKXMarginMode.ISOLATED,
            )
        },
        timeout_connection=30.0,
        timeout_reconciliation=30.0,
        timeout_portfolio=15.0,
        timeout_disconnection=10.0,
    )


def trader_is_running(node) -> bool:
    try:
        return bool(node.trader.is_running)
    except Exception:
        return False


def watch_startup(node, *, grace_s: float = 90.0, sleep=None, exit_fn=None) -> None:
    """Kill a node that came up but never started trading (L-02).

    nautilus logs and re-raises an exception from `on_start` and leaves the
    strategy in STARTING; the kernel's own failure branches simply `return`
    before `trader.start()`. Either way the process stays up and systemd reports
    `active` while nothing is subscribed, nothing is adopted and nothing trades.
    The only honest signal is to stop being up.
    """
    import time

    (sleep or time.sleep)(grace_s)
    if trader_is_running(node):
        return
    print(
        f"FATAL: the trader is not running {grace_s:.0f}s after start "
        f"(adoption refused, or connect/reconcile failed) -- exiting so systemd "
        f"restarts and the crash alert fires",
        file=sys.stderr,
        flush=True,
    )
    (exit_fn or os._exit)(3)


def run_exit_code(*, signalled: bool) -> int:
    """What `main` returns after `node.run()` comes back.

    `graceful_shutdown_on_exception=True` turns an internal error into a normal
    return, so exiting 0 told systemd the run succeeded: `Restart=on-failure`
    never restarted and the `OnFailure=` crash alert never fired (L-01). Only a
    signal (systemd stop / Ctrl-C) is a clean stop.
    """
    return 0 if signalled else 3


def main(argv: list[str] | None = None) -> int:
    from nautilus_trader.adapters.okx.factories import (
        OKXLiveDataClientFactory,
        OKXLiveExecClientFactory,
    )
    from nautilus_trader.live.node import TradingNode

    from ntx.config.settings import load
    from ntx.strategies.registry import get as get_strategy

    ap = argparse.ArgumentParser(description="run the ntx live/paper node")
    ap.add_argument("--strategy", default=None, help="default: NTX_STRATEGY")
    ap.add_argument("--instruments", default=None, help="comma bases, e.g. SOL,DOGE")
    ap.add_argument("--bar-spec", default="4-HOUR-LAST-EXTERNAL")
    ap.add_argument("--watchdog-minutes", type=int, default=None)
    ap.add_argument("--hold-bars", type=int, default=None, help="S900 only")
    ap.add_argument("--sl-price-pct", type=float, default=None, help="fixed stop, drills only")
    ap.add_argument(
        "--observe", action="store_true", help="connect and compute everything, never order"
    )
    ap.add_argument("--skip-preflight", action="store_true", help="demo only")
    ap.add_argument("--dry-run", action="store_true", help="plan only, do not connect")
    args = ap.parse_args(argv)

    # A configuration refusal (invalid .env, wrong key set, a live override, a
    # demo-only strategy on live) cannot be fixed by restarting: exit 2 keeps the
    # unit down and pages, where exit 1 was restarted for ~20 min first (E3-05).
    try:
        settings = load()
    except ValueError as e:
        print(f"REFUSING TO START: invalid settings: {e}", file=sys.stderr)
        return EXIT_REFUSED
    # The venue picks the account; arming picks whether real orders may be sent
    # at all. Both keys live in .env, so NTX_VENUE alone must not be enough.
    from ntx.live.arm import NotArmed, require_armed

    try:
        require_armed(settings, observe=args.observe, dry_run=args.dry_run)
    except NotArmed as e:
        print(f"REFUSING TO START: {e}", file=sys.stderr)
        return 2
    try:
        p = plan(
            settings,
            strategy_id=args.strategy or settings.strategy,
            bar_spec=args.bar_spec,
            instrument_ids=resolve_instruments(args.instruments) if args.instruments else None,
        )
    except (ValueError, RuntimeError, NotImplementedError) as e:
        print(f"REFUSING TO START: {e}", file=sys.stderr)
        return EXIT_REFUSED
    print(
        f"node plan: {'DEMO' if p.demo else 'LIVE'} {p.base_url_http} "
        f"strategy={p.strategy_id} bars={p.bar_spec} "
        f"instruments={','.join(p.instrument_ids)} journal={p.journal_path}"
    )
    if args.dry_run:
        return 0

    egress = resolve_egress()
    if not egress.allowed:
        print(f"REFUSING TO CONNECT: {egress.reason}", file=sys.stderr)
        # An unknown country is a failed lookup, not a restricted one: come back
        # and ask again rather than staying down until a human notices (L2-03).
        return EXIT_UNAVAILABLE if egress.country is None else EXIT_REFUSED
    if args.skip_preflight and not p.demo:
        # Preflight reads live specs, prices, equity and the min-notional screen.
        # Real money never starts without it (H-02).
        print("REFUSING: --skip-preflight is demo-only", file=sys.stderr)
        return 2
    if not args.skip_preflight:
        p, equity = run_preflight(p, settings)
        print(f"preflight: equity=${equity:,.2f} trading {','.join(p.instrument_ids)}", flush=True)
    # The venue's own leverage decides the liquidation price (L-07), and the
    # account mode decides whether any order is accepted at all (F055). Observe
    # mode cannot open a position, so it only reports.
    account_label = None
    try:
        assert_matches_gate(p, settings)
        account_label = assert_account_mode(p, settings)
        assert_funded(p, settings)
        assert_leverage(p, settings)
    except Unavailable as e:
        # Ordered before RuntimeError: Unavailable is one.
        if not args.observe:
            print(f"CANNOT START YET (will retry): {e}", file=sys.stderr)
            return EXIT_UNAVAILABLE
        print(f"OBSERVE: could not check -- {e}", flush=True)
    except RuntimeError as e:
        if not args.observe:
            print(f"REFUSING TO START: {e}", file=sys.stderr)
            return EXIT_REFUSED
        print(f"OBSERVE: would refuse to trade -- {e}", flush=True)

    extra = {}
    if args.watchdog_minutes is not None:
        extra["watchdog_minutes"] = args.watchdog_minutes
    if args.hold_bars is not None:
        extra["hold_bars"] = args.hold_bars
    if args.sl_price_pct is not None:
        extra.update(sl_price_pct=args.sl_price_pct, sl_atr_mult=None)
    if args.observe:
        extra["trading_enabled"] = False
    strat_cls, cfg_cls = get_strategy(p.strategy_id)
    strategy = strat_cls(cfg_cls(**strategy_kwargs(p, settings, **extra)))

    def venue_stop_hook(instrument_id: str) -> float | None:
        from ntx.ops.venue_stops import release_stop

        # An accepted cancel whose verification failed is reported, not raised
        # (L2-05): the replacement must still go out. journald is the right place
        # for it -- the alerter reads criticals from the journal DB, and this runs
        # inside on_start where the strategy's own journal may not be wired yet.
        return release_stop(
            instrument_id,
            settings,
            p.demo,
            on_ambiguity=lambda msg: print(f"ADOPTION AMBIGUITY: {msg}", flush=True),
        )

    def venue_stop_peek_hook(instrument_id: str):
        """Read the pre-restart stop WITHOUT cancelling it, for planning (L-10)."""
        from ntx.ops.venue_stops import peek_stop

        return peek_stop(instrument_id, settings, p.demo)

    def venue_position_hook():
        """What the venue says is held -- the check on a "closed while offline" (F066)."""
        from ntx.ops.venue_stops import venue_positions

        return venue_positions(settings, p.demo)

    def venue_stop_reap_hook(held):
        """Cancel close-position stops that guard no position (F065)."""
        from ntx.ops.venue_stops import reap_orphan_stops

        return reap_orphan_stops(held, settings, p.demo)

    def equity_hook() -> float | None:
        """The venue's OWN equity figure (T2-01).

        `balance_total(USDT)` is available cash on OKX: the isolated margin of
        an open position is not in it, and it does not move with price. Reading
        it as equity makes a normal book look like a loss. `details[USDT].eq` is
        the number OKX itself calls equity, and it is exactly what preflight
        already sizes from (`usdt_equity`).
        """
        from ntx.ops.okx_rest import signed

        body = signed("GET", p.base_url_http, "/api/v5/account/balance", settings.creds(), p.demo)
        if str(body.get("code")) != "0":
            raise RuntimeError(f"balance read failed: {body.get('code')} {body.get('msg')}")
        return usdt_equity(body)

    strategy.equity_hook = equity_hook
    strategy.account_label = account_label

    # Peeking is read-only, so observe mode may do it; cancelling is not (R48).
    strategy.venue_stop_peek_hook = venue_stop_peek_hook
    # Read-only, so observe mode uses it too: it only ever prevents an action.
    strategy.venue_position_hook = venue_position_hook
    if not args.observe:  # observe mode must never cancel a live stop
        strategy.venue_stop_hook = venue_stop_hook
        strategy.venue_stop_reap_hook = venue_stop_reap_hook

    from ntx.live.lock import AlreadyRunning, single_instance

    try:
        lock = single_instance(p.trader_id, settings.data_dir, demo=p.demo)
        lock.__enter__()
    except AlreadyRunning as e:
        print(f"REFUSING TO START: {e}", file=sys.stderr)
        return 2

    node = TradingNode(config=node_config(p, settings))
    node.trader.add_strategy(strategy)
    node.add_data_client_factory(VENUE_NAME, OKXLiveDataClientFactory)
    node.add_exec_client_factory(VENUE_NAME, OKXLiveExecClientFactory)
    node.build()

    # L-01: a signalled stop (systemd / Ctrl-C) is the only clean one. Wrap the
    # kernel's own signal callback rather than installing handlers of our own,
    # so the proven SIGTERM shutdown path is untouched.
    signalled = {"seen": False}
    _kernel_sig = node.kernel.loop_sig_callback

    def _note_signal(sig):
        signalled["seen"] = True
        if _kernel_sig is not None:
            _kernel_sig(sig)

    node.kernel._loop_sig_callback = _note_signal

    import threading

    threading.Thread(target=watch_startup, args=(node,), daemon=True).start()

    try:
        node.run()
    except KeyboardInterrupt:
        signalled["seen"] = True
    finally:
        node.dispose()
        lock.__exit__(None, None, None)
        if needs_post_run_flatten(p):
            from ntx.ops.flatten import main as flatten

            print("post-run: flattening demo account", flush=True)
            bases = ",".join(i.split("-")[0] for i in p.instrument_ids)
            flatten(["--demo", "--only", bases])
    rc = run_exit_code(signalled=signalled["seen"])
    if rc:
        print(
            "node stopped without a signal -- treating as a failure so systemd "
            "restarts and the crash alert fires",
            file=sys.stderr,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
