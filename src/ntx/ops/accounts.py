"""What each OKX key can actually see: which account, how much USDT, and whether
this box's IPv6 address is on the key's IP whitelist.

Both questions used to need a human logging into the OKX web app, which meant
neither was ever measured (R11):

* **Which account, and how much.** The keys belong to sub-accounts (ADR-0014),
  while the OKX app opens on the MAIN account -- so "$106K in the app, $0 in the
  bot" looks like a bug and is not one (F059). And the demo soak sizes from
  `NTX_EQUITY_OVERRIDE_USD`, which once hid a completely empty demo account for
  a day.
* **Whether IPv6 is whitelisted.** F061: a signed call left over IPv6 and OKX
  refused it with 50110. `ntx.ops.okx_rest` now pins IPv4, but the nautilus OKX
  adapter's own HTTP client -- the one that sends orders -- cannot be pinned
  from config. So if this box can reach OKX over IPv6 at all, that address has
  to be on the whitelist, and `--check-ipv6` is how we find out rather than
  believing it.

    python -m ntx.ops.accounts                      # every key set in .env
    python -m ntx.ops.accounts --live --check-ipv6  # the whitelist acceptance test
    python -m ntx.ops.accounts --json

Exit codes: **0** everything read and nothing wrong · **1** something is wrong
(the key does not authenticate, the account mode would refuse our orders, the
balance could not be read, the IPv6 address is not whitelisted) · **2** the tool
could not run at all (no keys, no answer from the venue).

Read-only: GET only, no order, no cancel, no setting changed. There is
deliberately no egress gate here -- "what does the venue see from where I am
standing" is exactly the question you need answered from a host you would not
trade from.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from dataclasses import asdict, dataclass

from ntx.config.settings import Settings, load
from ntx.config.venues import OKX
from ntx.live.node import account_mode_problem, describe_account, usdt_equity
from ntx.ops.okx_rest import signed

CONFIG_PATH = "/api/v5/account/config"
BALANCE_PATH = "/api/v5/account/balance"
IP_WHITELIST_CODE = "50110"

# Probed before the signed IPv6 call so that "OKX refused us" and "nothing left
# this box at all" are told apart. `curl --ipv6` is silent about both, and a
# check that cannot tell them apart reports absence when it means "I could not
# tell" (R70).
PROBE_HOST = "www.okx.com"
PROBE_PORT = 443
PROBE_TIMEOUT_S = 5.0

# An IPv6 or IPv4 literal inside an OKX error message, e.g.
# "Your IP 2a01:4f9:c015:6148::1 is not included in your API key's IP whitelist".
_IP_IN_MESSAGE = re.compile(r"((?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}|(?:\d{1,3}\.){3}\d{1,3})")


def for_keyset(settings: Settings, keyset: str) -> Settings:
    """The same settings pointed at an alternate DEMO sub-account (R51).

    A second strategy needs a second sub-account; `NTX_KEYSET` selects it. This
    exists so `--demo2` can be checked before a node is ever pointed at it.
    """
    return settings.model_copy(update={"venue": "okx_demo", "keyset": keyset})


def for_env(settings: Settings, demo: bool) -> Settings:
    """Settings whose `creds()` match the environment we are about to call.

    `creds()` picks by `NTX_VENUE` while `demo` sets the `x-simulated-trading`
    header; letting those two disagree is OKX 50101 "APIKey does not match
    current environment" (F050).
    """
    return settings.model_copy(update={"venue": "okx_demo" if demo else "okx_live"})


def has_creds(settings: Settings, demo: bool) -> bool:
    return all(for_env(settings, demo).creds())


def scrub(text: str, creds: tuple[str, str, str]) -> str:
    """Never let a venue message carry a credential back out to a terminal (R1).

    OKX does not echo keys in its error text today. This output gets pasted into
    chat by whoever ran it on the box, and "today" is not a guarantee worth a
    leaked credential.
    """
    for value in creds:
        if value:
            text = text.replace(value, "<redacted>")
    return text


def usdt_cash(balance: dict) -> float:
    """The USDT the account HOLDS, as opposed to what it is worth (R66).

    `cashBal` does not move with an open position's PnL; `eq` does. Printing
    both is what makes "the balance never changed but equity fell" legible
    instead of alarming (F057).
    """
    for d in balance["data"][0].get("details", []):
        if d.get("ccy") == "USDT":
            return float(d.get("cashBal") or 0.0)
    return 0.0


@dataclass(frozen=True)
class Account:
    """One key set's view of its account. `None` means "not read", never zero."""

    env: str
    authenticated: bool
    account: str = ""
    uid: str = ""
    label: str = ""
    acct_lv: str = ""
    pos_mode: str = ""
    usdt_balance: float | None = None
    usdt_equity: float | None = None
    problem: str = ""
    unreachable: bool = False

    def lines(self) -> list[str]:
        if not self.authenticated:
            return [f"{self.env:<5} {self.problem}"]
        head = (
            f"{self.env:<5} {self.account} uid={self.uid or '?'} "
            f"acctLv={self.acct_lv or '?'} posMode={self.pos_mode or '?'}"
        )
        if self.usdt_equity is None:
            # R18/R31: an error body carries no balance. "$0.00" here would read
            # as "the account was drained" during exactly the wrong five minutes.
            money = f"balance NOT read -- {self.problem}"
        else:
            money = f"USDT balance ${self.usdt_balance:,.2f} | equity ${self.usdt_equity:,.2f}"
            if self.problem:
                money += f" | {self.problem}"
        return [head, f"      {money}"]


def read_account(settings: Settings, demo: bool) -> Account:
    """One authenticated config read and one balance read. Never raises."""
    env = "demo" if demo else "live"
    creds = for_env(settings, demo).creds()
    cfg = signed("GET", OKX.auth_base_url, CONFIG_PATH, creds, demo)
    if cfg.get("_err"):
        detail = scrub(str(cfg["_err"]), creds)
        return Account(env, False, problem=f"no answer from OKX: {detail}", unreachable=True)
    if str(cfg.get("code")) != "0":
        why = scrub(f"{cfg.get('code')} {cfg.get('msg')}", creds)
        return Account(env, False, problem=f"key does NOT authenticate: {why}")

    config = (cfg.get("data") or [{}])[0]
    problems: list[str] = []
    # An account this key cannot place our orders from is a problem worth an exit
    # code, not a detail: acctLv=1 looked healthy for three hours of soak (F055).
    mode = account_mode_problem(config)
    if mode:
        problems.append(mode)
    bal = signed("GET", OKX.auth_base_url, BALANCE_PATH, creds, demo)
    cash: float | None = None
    equity: float | None = None
    if bal.get("_err"):
        problems.append(f"no answer from OKX: {scrub(str(bal['_err']), creds)}")
    elif str(bal.get("code")) != "0":
        why = scrub(f"{bal.get('code')} {bal.get('msg')}", creds)
        problems.append(f"balance read failed: {why}")
    else:
        try:
            cash, equity = usdt_cash(bal), usdt_equity(bal)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            problems.append(f"balance body not understood: {type(e).__name__}")
    return Account(
        env=env,
        authenticated=True,
        account=describe_account(config),
        uid=str(config.get("uid") or ""),
        label=str(config.get("label") or ""),
        acct_lv=str(config.get("acctLv") or ""),
        pos_mode=str(config.get("posMode") or ""),
        usdt_balance=cash,
        usdt_equity=equity,
        problem="; ".join(problems),
    )


def ipv6_source_address(
    host: str = PROBE_HOST, port: int = PROBE_PORT, timeout: float = PROBE_TIMEOUT_S
) -> str | None:
    """The local IPv6 address a call to `host` would leave from, or None.

    This is the address OKX would see, measured rather than read off `ip -6 a`:
    a box can hold an IPv6 address and still have no route to the venue.
    """
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return None
    for *_, sockaddr in infos:
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(sockaddr)
                return str(sock.getsockname()[0])
        except OSError:
            continue
    return None


@dataclass(frozen=True)
class Ipv6Check:
    status: str  # whitelisted | not_whitelisted | no_route | error
    address: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        # "no route" is a fine state: nothing can leave over IPv6, so nothing
        # can be refused for it. "error" is not -- we could not tell (R70).
        return self.status in ("whitelisted", "no_route")

    def line(self) -> str:
        if self.status == "whitelisted":
            return "IPv6 whitelisted (signed call succeeded over IPv6)"
        if self.status == "not_whitelisted":
            return f"NOT whitelisted — OKX returned {IP_WHITELIST_CODE} for {self.address}"
        if self.status == "no_route":
            return "no IPv6 route from this host"
        return self.detail


def check_ipv6(settings: Settings, demo: bool) -> Ipv6Check:
    """Repeat the balance read forced over IPv6 and report what OKX said (F061)."""
    address = ipv6_source_address()
    if address is None:
        return Ipv6Check("no_route")
    creds = for_env(settings, demo).creds()
    body = signed("GET", OKX.auth_base_url, BALANCE_PATH, creds, demo, family="ipv6")
    if body.get("_err"):
        detail = scrub(str(body["_err"]), creds)
        return Ipv6Check("error", address, f"IPv6 call to OKX failed from {address}: {detail}")
    code = str(body.get("code"))
    msg = scrub(str(body.get("msg") or ""), creds)
    if code == "0":
        return Ipv6Check("whitelisted", address)
    if code == IP_WHITELIST_CODE:
        found = _IP_IN_MESSAGE.search(msg)
        return Ipv6Check("not_whitelisted", found.group(1) if found else address)
    return Ipv6Check("error", address, f"IPv6 call from {address} returned {code} {msg}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m ntx.ops.accounts",
        description=(
            "Which OKX account each key belongs to, what it holds, and whether "
            "this box's IPv6 address is on the key's whitelist. Read-only."
        ),
    )
    env = ap.add_mutually_exclusive_group()
    env.add_argument("--demo", action="store_true", help="only the demo key set")
    env.add_argument("--live", action="store_true", help="only the live key set")
    ap.add_argument("--json", action="store_true", help="machine-readable, same facts")
    ap.add_argument(
        "--check-ipv6",
        action="store_true",
        help="repeat the balance read forced over IPv6 and report the whitelist verdict (F061)",
    )
    args = ap.parse_args(argv)

    settings = load()
    wanted = [True] if args.demo else [False] if args.live else [True, False]
    envs = [demo for demo in wanted if has_creds(settings, demo)]
    if not envs:
        which = "demo" if args.demo else "live" if args.live else "demo or live"
        print(f"no {which} OKX keys in .env -- nothing to read", file=sys.stderr)
        return 2

    reports = [read_account(settings, demo) for demo in envs]
    checks = {
        ("demo" if demo else "live"): check_ipv6(settings, demo)
        for demo in (envs if args.check_ipv6 else [])
    }

    if args.json:
        print(
            json.dumps(
                {
                    "accounts": [asdict(a) for a in reports],
                    "ipv6": {k: {**asdict(v), "line": v.line()} for k, v in checks.items()},
                },
                indent=2,
            )
        )
    else:
        for a in reports:
            for line in a.lines():
                print(line)
        for env_name, check in checks.items():
            print(f"{env_name:<5} {check.line()}")

    # "Nothing answered" and "the venue refused me" are opposite facts: only the
    # first one means the check did not run.
    if all(a.unreachable for a in reports):
        return 2
    wrong = any(a.problem for a in reports) or any(not c.ok for c in checks.values())
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
