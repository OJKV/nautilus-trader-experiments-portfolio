"""`make doctor` -- prove the environment before trusting it.

Checks versions, per-host reachability with BOTH a default and a browser
User-Agent (so a client-fingerprint ban can never again be misread as a
geo-block -- see F006), credential presence (never values), account-mode parity
between live and demo, key age, and required paths.

Run with --auth to include authenticated OKX calls (needs the VPN on from KZ).
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ntx.config import venues
from ntx.config.settings import REPO_ROOT, load

OK, WARN, FAIL, INFO = "  ok  ", " warn ", " FAIL ", " info "
_results: list[tuple[str, str]] = []


def report(status: str, msg: str) -> None:
    _results.append((status, msg))
    print(f"[{status}] {msg}")


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# --------------------------------------------------------------------------- #
def check_versions() -> None:
    section("versions")
    report(OK, f"python {sys.version.split()[0]}")
    try:
        import nautilus_trader

        report(OK, f"nautilus_trader {nautilus_trader.__version__}")
    except ImportError:
        report(FAIL, "nautilus_trader not importable -- run `make install`")


def _http(url: str, ua: str | None, timeout: int = 8) -> tuple[int | None, str]:
    """Plain GET. Returns (status, short body). Never raises."""
    headers = {"User-Agent": ua} if ua else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(160).decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(160).decode(errors="replace")
    except Exception as e:
        return None, f"{type(e).__name__}"


def check_hosts(auth_required: bool = False) -> None:
    """Probe each host with a default and a browser UA.

    The UA pair matters: OKX's Cloudflare returns 403 "error code: 1010" for
    default library User-Agents. Seeing both results side by side is what stops
    that being mistaken for a network or geographic block (F006).
    """
    section("okx hosts (public endpoint, default UA vs browser UA)")
    for host in venues.OKX_HOSTS:
        url = f"{host.url}/api/v5/public/time"
        d_status, _ = _http(url, None)
        b_status, b_body = _http(url, venues.BROWSER_UA)
        tag = f"{host.url:<24}"
        if b_status == 200 and d_status == 200:
            report(OK, f"{tag} reachable with either UA")
        elif b_status == 200:
            report(
                OK,
                f"{tag} reachable ONLY with a browser UA "
                f"(default UA -> {d_status}) -- expected, see F006",
            )
        elif b_status is None:
            # The authenticated host is TCP-blocked from Kazakhstan without a VPN.
            # That is expected and harmless for backtesting/preflight, so it is
            # only a hard failure when this run actually needs authentication.
            if host.authenticated and auth_required:
                report(
                    FAIL,
                    f"{tag} unreachable ({b_body}) -- authenticated run needs "
                    f"this host: turn the VPN on",
                )
            elif host.authenticated:
                report(
                    WARN,
                    f"{tag} unreachable ({b_body}) -- expected with the VPN off; "
                    f"needed only for authenticated runs",
                )
            else:
                report(WARN, f"{tag} unreachable ({b_body})")
        else:
            report(FAIL, f"{tag} HTTP {b_status}: {b_body[:80]}")


def check_creds() -> None:
    """Presence only -- never print or log a key value (R1)."""
    section("credentials (presence only)")
    s = load()
    groups = {
        "OKX live": (s.okx_api_key, s.okx_api_secret, s.okx_api_passphrase),
        "OKX demo": (s.okx_demo_api_key, s.okx_demo_api_secret, s.okx_demo_api_passphrase),
    }
    for name, vals in groups.items():
        have = sum(bool(v) for v in vals)
        if have == 3:
            report(OK, f"{name}: key+secret+passphrase set")
        elif have == 0:
            report(INFO, f"{name}: not configured")
        else:
            report(FAIL, f"{name}: incomplete ({have}/3 set)")
    if s.telegram_enabled:
        report(OK, "telegram: token+chat_id set")
    else:
        report(INFO, "telegram: deferred (alerts disabled; not a blocker)")


def check_paths() -> None:
    """Validate paths at startup, not at first use (R21)."""
    section("paths (absolute, validated now not on first use)")
    s = load()
    for p in s.required_paths():
        if not p.is_absolute():
            report(FAIL, f"{p} is not absolute")
        elif p.exists():
            report(OK, str(p.relative_to(REPO_ROOT)) if REPO_ROOT in p.parents else str(p))
        else:
            p.mkdir(parents=True, exist_ok=True)
            report(WARN, f"created missing {p}")


def check_config() -> None:
    section("settings")
    try:
        s = load()
    except Exception as e:
        report(FAIL, f"settings invalid: {e}")
        return
    report(OK, f"venue={s.venue} strategy={s.strategy} trader_id={s.trader_id}")
    report(
        OK,
        f"risk_equity_pct={s.risk_equity_pct} max_leverage={s.max_leverage} "
        f"max_positions={s.max_concurrent_positions}",
    )
    report(
        OK,
        f"daily_loss_limit_equity_pct={s.daily_loss_limit_equity_pct} "
        f"max_drawdown_equity_pct={s.max_drawdown_equity_pct}",
    )
    if s.equity_override_usd:
        report(INFO, f"equity override ${s.equity_override_usd:.2f} (demo parity)")
    spec = venues.get("OKX")
    report(
        INFO,
        f"OKX caps: native_tsl={spec.native_trailing_stop} "
        f"attaches_stop_at_entry={spec.attaches_stop_at_entry} (F007)",
    )


# --------------------------------------------------------------------------- #
def _signed_get(base: str, path: str, creds: tuple[str, str, str], demo: bool) -> dict:
    """Authenticated GET. Credentials travel on curl's stdin, never in the argv
    (L-14) -- see `ntx.ops.okx_rest`."""
    from ntx.ops.okx_rest import signed

    return signed("GET", base, path, creds, demo)


def secret_file_mode_ok(path: Path) -> bool:
    """True if a secrets file is not readable by group or others (M-04).

    Trade-capable API credentials in a 0644 file can be read by any local
    account on the host.
    """
    return not (path.stat().st_mode & 0o077)


ACCT_LV = {
    "1": "Simple",
    "2": "Single-currency margin",
    "3": "Multi-currency margin",
    "4": "Portfolio margin",
}


def check_auth() -> None:
    """Authenticated checks. Requires the VPN on when run from Kazakhstan.

    The egress country is verified FIRST. Proton VPN Free assigns one of ten
    countries without letting free users choose, and three (CA, JP, US) are
    OKX-restricted -- so no signed request is sent until the exit is known-good.
    """
    from ntx.config.geo import check_egress

    section("authenticated okx (requires VPN from KZ)")
    egress = check_egress()
    if not egress.allowed:
        report(FAIL, f"egress {egress.country}: {egress.reason}")
        report(INFO, "no authenticated OKX calls were sent")
        return
    report(WARN if egress.eea else OK, f"egress {egress.country} ({egress.colo}): {egress.reason}")
    s = load()
    spec = venues.get("OKX")
    envs = [("live", (s.okx_api_key, s.okx_api_secret, s.okx_api_passphrase), False)]
    if s.okx_demo_api_key:
        envs.append(
            ("demo", (s.okx_demo_api_key, s.okx_demo_api_secret, s.okx_demo_api_passphrase), True)
        )

    modes: dict[str, str] = {}
    for name, creds, demo in envs:
        if not all(creds):
            report(INFO, f"{name}: not configured, skipped")
            continue
        cfg = _signed_get(spec.auth_base_url, "/api/v5/account/config", creds, demo)
        if cfg.get("code") != "0":
            report(
                FAIL, f"{name} auth: {cfg.get('msg') or cfg.get('_err')} (code={cfg.get('code')})"
            )
            continue
        c = cfg["data"][0]
        modes[name] = c.get("acctLv", "?")
        report(
            OK,
            f"{name} auth OK | acctLv={c.get('acctLv')} "
            f"({ACCT_LV.get(str(c.get('acctLv')), '?')}) posMode={c.get('posMode')}",
        )
        # Printing a number nobody compares is not a check: a demo account in
        # Spot mode looked fine here while the venue refused every order (F055).
        from ntx.live.node import account_mode_problem, describe_account

        report(OK, f"{name} key belongs to the {describe_account(c)}")
        problem = account_mode_problem(c)
        if problem:
            report(FAIL, f"{name} cannot place our orders -- {problem}")
        else:
            report(OK, f"{name} account mode can place margined SWAP orders")

        perm = c.get("perm", "")
        if "withdraw" in perm.lower():
            report(FAIL, f"{name} key has WITHDRAWAL permission -- revoke and recreate (R2)")
        else:
            report(OK, f"{name} key perm={perm} (no withdrawal)")

        if not c.get("ip"):
            report(
                WARN,
                f"{name} key is not IP-bound -- OKX auto-deletes idle "
                f"non-IP-bound trade keys after 14 days",
            )

        bal = _signed_get(spec.auth_base_url, "/api/v5/account/balance", creds, demo)
        if bal.get("code") == "0" and bal["data"]:
            report(OK, f"{name} equity=${float(bal['data'][0].get('totalEq') or 0):,.2f}")

        fee = _signed_get(spec.auth_base_url, spec.fee_endpoint, creds, demo)
        if fee.get("code") == "0" and fee["data"]:
            f = fee["data"][0]
            report(
                OK,
                f"{name} fees maker={float(f['maker']) * 100:+.4f}% "
                f"taker={float(f['taker']) * 100:+.4f}% (fetched, never hardcoded)",
            )

    if len(modes) == 2 and modes["live"] != modes["demo"]:
        report(
            FAIL,
            f"account-mode MISMATCH: live acctLv={modes['live']} "
            f"({ACCT_LV.get(modes['live'], '?')}) vs demo acctLv={modes['demo']} "
            f"({ACCT_LV.get(modes['demo'], '?')}) -- demo will not represent live",
        )
    elif len(modes) == 2:
        report(OK, f"live and demo account modes match (acctLv={modes['live']})")


def main() -> int:
    ap = argparse.ArgumentParser(description="ntx environment doctor")
    ap.add_argument("--auth", action="store_true", help="include authenticated OKX checks")
    args = ap.parse_args()

    print("\033[1mntx doctor\033[0m")
    check_versions()
    check_config()
    check_paths()
    check_creds()
    check_hosts(auth_required=args.auth)
    if args.auth:
        check_auth()
    else:
        print("\n(run `make doctor-vpn` for authenticated checks -- needs the VPN on)")

    fails = sum(1 for s, _ in _results if s == FAIL)
    warns = sum(1 for s, _ in _results if s == WARN)
    print(f"\n{'=' * 60}\n{len(_results)} checks | {fails} FAIL | {warns} warn")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
