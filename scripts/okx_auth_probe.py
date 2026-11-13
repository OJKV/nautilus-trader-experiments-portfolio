"""Read-only OKX probe. Never prints key material.

Usage: python3 scripts/okx_auth_probe.py [--demo] [--base https://okx.com]
"""
import base64, hashlib, hmac, json, sys, urllib.request, urllib.error
from datetime import datetime, timezone

DEMO = "--demo" in sys.argv
BASE_OVERRIDE = None
if "--base" in sys.argv:
    BASE_OVERRIDE = sys.argv[sys.argv.index("--base") + 1]

ENV = {}
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k.strip()] = v.strip()

pfx = "OKX_DEMO_" if DEMO else "OKX_"
KEY, SECRET, PASS = ENV.get(f"{pfx}API_KEY",""), ENV.get(f"{pfx}API_SECRET",""), ENV.get(f"{pfx}API_PASSPHRASE","")
BASE = (BASE_OVERRIDE or ENV.get("OKX_BASE_URL_HTTP","https://okx.com")).rstrip("/")
if not (KEY and SECRET and PASS):
    sys.exit(f"MISSING {pfx}API_KEY / _SECRET / _PASSPHRASE in .env")
print(f"### {'DEMO' if DEMO else 'LIVE'} key  |  base={BASE}  |  key=***{KEY[-4:]}")

def get(path):
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond//1000:03d}Z"
    sign = base64.b64encode(hmac.new(SECRET.encode(), (ts+"GET"+path).encode(), hashlib.sha256).digest()).decode()
    req = urllib.request.Request(BASE+path, headers={
        "OK-ACCESS-KEY": KEY, "OK-ACCESS-SIGN": sign, "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASS, "Content-Type": "application/json",
        "x-simulated-trading": "1" if DEMO else "0",
        # REQUIRED: Cloudflare returns 403 "error code: 1010" for the default
        # Python-urllib User-Agent on okx hosts. Not a geo-block. See F005.
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, e.read().decode(errors="replace")[:200]
    except Exception as e: return None, f"{type(e).__name__}: {e}"

ACCT = {"1":"Simple","2":"Single-currency margin","3":"Multi-currency margin","4":"Portfolio margin"}
for label, path in [("account config","/api/v5/account/config"),
                    ("balance","/api/v5/account/balance"),
                    ("fee rate SWAP","/api/v5/account/trade-fee?instType=SWAP&instId=BTC-USDT-SWAP"),
                    ("positions","/api/v5/account/positions"),
                    ("max size SOL iso","/api/v5/account/max-size?instId=SOL-USDT-SWAP&tdMode=isolated"),
                    ("leverage SOL iso","/api/v5/account/leverage-info?instId=SOL-USDT-SWAP&mgnMode=isolated")]:
    st, body = get(path)
    print(f"  --- {label} [HTTP {st}]")
    if not isinstance(body, dict): print(f"      {body}"); continue
    if body.get("code") != "0": print(f"      code={body.get('code')} msg={body.get('msg')}"); continue
    d = body.get("data", [])
    if label == "account config" and d:
        c = d[0]
        print(f"      acctLv={c.get('acctLv')} ({ACCT.get(str(c.get('acctLv')),'?')})  posMode={c.get('posMode')}")
        print(f"      kycLv={c.get('kycLv')} level={c.get('level')} ctIsoMode={c.get('ctIsoMode')} roleType={c.get('roleType')}")
        perm = c.get('perm'); print(f"      key perm={perm}")
    elif label == "balance" and d:
        b = d[0]; print(f"      totalEq=${float(b.get('totalEq') or 0):.2f}  imr={b.get('imr') or '-'} mmr={b.get('mmr') or '-'}")
        for c in b.get("details", []):
            if float(c.get("eq") or 0) > 0:
                print(f"        {c['ccy']}: eq={c['eq']}  availBal={c.get('availBal')}  availEq={c.get('availEq')}")
    elif label == "fee rate SWAP" and d:
        f = d[0]; print(f"      maker={float(f['maker'])*100:+.4f}%  taker={float(f['taker'])*100:+.4f}%   (negative = you pay)")
    elif label == "positions": print(f"      open positions: {len(d)}")
    elif label == "max size SOL iso" and d: print(f"      maxBuy={d[0].get('maxBuy')} maxSell={d[0].get('maxSell')} contracts")
    elif label == "leverage SOL iso" and d: print(f"      lever={d[0].get('lever')}x mgnMode={d[0].get('mgnMode')}")
print()
