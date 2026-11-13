"""One signed OKX REST call, with the credentials kept off the process list.

`curl` is used deliberately (F006): it sends an accepted User-Agent and matches
what was verified by hand in Phase 0. But passing headers as `-H` puts the API
key and the passphrase in `/proc/<pid>/cmdline`, which is world-readable — and
this VPS is shared with another trading project running as a different user
(review L-14). So every option, including the headers, is written to curl's
stdin with `--config -`, which no other process can read.

The HMAC signature is *not* a secret worth hiding: it is derived per request,
covers the timestamp, and is useless a few seconds later. The key and the
passphrase are long-lived, and the API secret never leaves this process at all —
only the signature it produces.

    signed("GET",  base, "/api/v5/account/balance", creds, demo)
    signed("POST", base, "/api/v5/account/set-leverage", creds, demo, body={...})
    signed("GET",  base, "/api/v5/account/balance", creds, demo, family="ipv6")
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
from datetime import UTC, datetime

from ntx.config import venues

TIMEOUT_S = 20


def _okx_timestamp() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sign(secret: str, ts: str, method: str, path: str, payload: str) -> str:
    msg = (ts + method.upper() + path + payload).encode()
    return base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()


def _quote(value: str) -> str:
    """Escape a value for a curl config file (a quoted string)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


# `ipv4` (curl -4), not a preference: the OKX keys are bound to this box's IPv4
# address, and the box also has a public IPv6 one. /etc/gai.conf only *prefers*
# IPv4, so a call occasionally left over IPv6 and OKX refused it with 50110 "not
# included in your API key's IP whitelist" (F061). The same helper carries the
# flatten kill switch and the stop release on restart, so ipv4 is the default and
# stays the default. `ipv6` exists for one caller only -- `ntx.ops.accounts`,
# which asks the venue whether that address is whitelisted instead of asking a
# human to read the OKX web app. `auto` is curl with no flag at all.
FAMILY_FLAG = {"ipv4": "ipv4", "ipv6": "ipv6", "auto": None}


def _config(
    url: str,
    method: str,
    headers: dict[str, str],
    payload: str | None,
    family: str = "ipv4",
) -> str:
    if family not in FAMILY_FLAG:
        # Never fall back to the resolver's choice: that silence *was* F061.
        raise ValueError(f"family must be one of {sorted(FAMILY_FLAG)}, not {family!r}")
    lines = [f'url = "{_quote(url)}"', "silent", f"max-time = {TIMEOUT_S}"]
    flag = FAMILY_FLAG[family]
    if flag:
        lines.insert(2, flag)
    if method.upper() != "GET":
        lines.append(f'request = "{_quote(method.upper())}"')
    lines += [f'header = "{_quote(f"{k}: {v}")}"' for k, v in headers.items()]
    if payload is not None:
        lines.append(f'data = "{_quote(payload)}"')
    return "\n".join(lines) + "\n"


def signed(
    method: str,
    base: str,
    path: str,
    creds: tuple[str, str, str],
    demo: bool,
    body: dict | None = None,
    family: str = "ipv4",
) -> dict:
    """Authenticated OKX call. Returns parsed JSON, or {"_err": ...}.

    `family` pins the address family of the call: "ipv4" (the default, F061),
    "ipv6" or "auto".
    """
    key, secret, passphrase = creds
    # Compact separators: the HMAC covers the literal bytes that are sent, so any
    # reformatting of the body invalidates the signature.
    payload = json.dumps(body, separators=(",", ":")) if body is not None else None
    ts = _okx_timestamp()
    headers = {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": _sign(secret, ts, method, path, payload or ""),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "x-simulated-trading": "1" if demo else "0",
        "User-Agent": venues.BROWSER_UA,
    }
    config = _config(base + path, method, headers, payload, family=family)
    out = subprocess.run(
        ["curl", "--config", "-"],
        input=config,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(out.stdout)
    except Exception:
        return {"_err": (out.stdout or out.stderr or "empty response")[:160]}
