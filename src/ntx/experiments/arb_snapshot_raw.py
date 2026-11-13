# ruff: noqa
"""Raw cross-venue market snapshot (public endpoints only). Run via arb_snapshot.py."""

import json, urllib.request, datetime as dt, re

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def get(u):
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20))
    except Exception as e:
        print("ERR", u[:70], str(e)[:60])
        return None


SYMS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "LINK", "AVAX", "TRX"]
now = dt.datetime.now(dt.timezone.utc)
# Binance perp funding
bn = {x["symbol"]: x for x in get("https://fapi.binance.com/fapi/v1/premiumIndex") or []}
fi = {
    x["symbol"]: x.get("fundingIntervalHours", 8)
    for x in get("https://fapi.binance.com/fapi/v1/fundingInfo") or []
}
# Bybit
bt = {
    x["symbol"]: x
    for x in (get("https://api.bybit.com/v5/market/tickers?category=linear") or {})
    .get("result", {})
    .get("list", [])
}
bi = {
    x["symbol"]: int(x.get("fundingInterval", 480))
    for x in (
        get("https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000") or {}
    )
    .get("result", {})
    .get("list", [])
    if "fundingInterval" in x
}
print(
    "FUNDING annualised %  (rate x 8760/interval_h), one snapshot", now.strftime("%Y-%m-%d %H:%MZ")
)
print("sym   binance(int)   okx(int)   bybit(int)   max-min")
rows = []
for s in SYMS:
    out = {}
    b = bn.get(s + "USDT")
    if b and b.get("lastFundingRate") not in (None, ""):
        h = fi.get(s + "USDT", 8)
        out["bn"] = (float(b["lastFundingRate"]) * 8760 / h * 100, h)
    o = get(f"https://my.okx.com/api/v5/public/funding-rate?instId={s}-USDT-SWAP")
    if o and o.get("data"):
        d = o["data"][0]
        h = (int(d["nextFundingTime"]) - int(d["fundingTime"])) / 3600000
        out["okx"] = (float(d["fundingRate"]) * 8760 / h * 100, h)
    y = bt.get(s + "USDT")
    if y and y.get("fundingRate"):
        h = bi.get(s + "USDT", 480) / 60
        out["by"] = (float(y["fundingRate"]) * 8760 / h * 100, h)
    vals = [v[0] for v in out.values()]
    sp = max(vals) - min(vals) if len(vals) > 1 else float("nan")
    rows.append((s, out, sp))
    print(
        f"{s:5}",
        " ".join(f"{k}:{v[0]:7.2f}({v[1]:.0f}h)" for k, v in out.items()),
        f" spread {sp:6.2f}",
    )
# spot cross-venue mids
print("\nSPOT mid gap across venues, bps (one snapshot)")
bs = {x["symbol"]: x for x in get("https://api.binance.com/api/v3/ticker/bookTicker") or []}
bys = {
    x["symbol"]: x
    for x in (get("https://api.bybit.com/v5/market/tickers?category=spot") or {})
    .get("result", {})
    .get("list", [])
}
for s in SYMS:
    m = {}
    x = bs.get(s + "USDT")
    if x:
        m["bn"] = (float(x["bidPrice"]) + float(x["askPrice"])) / 2
    o = get(f"https://my.okx.com/api/v5/market/ticker?instId={s}-USDT")
    if o and o.get("data"):
        d = o["data"][0]
        m["okx"] = (float(d["bidPx"]) + float(d["askPx"])) / 2
    y = bys.get(s + "USDT")
    if y and y.get("bid1Price"):
        m["by"] = (float(y["bid1Price"]) + float(y["ask1Price"])) / 2
    if len(m) > 1:
        v = list(m.values())
        print(
            f"{s:5} max-min gap {(max(v) / min(v) - 1) * 1e4:6.2f} bps",
            {k: round(x, 4) for k, x in m.items()},
        )
# dated futures basis
print("\nDATED FUTURES annualised basis vs index (mark/index-1)*365/days, one snapshot")


def ann(mark, idx, exp):
    days = (exp - now).total_seconds() / 86400
    return ((mark / idx - 1) * 365 / days * 100, days)


for k, v in bn.items():
    m = re.match(r"(BTC|ETH)USDT_(\d{6})$", k)
    if m:
        exp = dt.datetime.strptime(m.group(2), "%y%m%d").replace(hour=8, tzinfo=dt.timezone.utc)
        a, d = ann(float(v["markPrice"]), float(v["indexPrice"]), exp)
        print(f"binance {k} {a:6.2f}%  ({d:.0f}d)")
for k, v in bt.items():
    m = re.match(r"(BTC|ETH)-(\d{2})([A-Z]{3})(\d{2})$", k)
    if m and v.get("indexPrice"):
        exp = dt.datetime.strptime(m.group(2) + m.group(3) + m.group(4), "%d%b%y").replace(
            hour=8, tzinfo=dt.timezone.utc
        )
        a, d = ann(float(v["markPrice"]), float(v["indexPrice"]), exp)
        print(f"bybit   {k} {a:6.2f}%  ({d:.0f}d)")
for inst, idx in (
    ("BTC-USD_UM-261225", "BTC-USD"),
    ("BTC-USD_UM-270326", "BTC-USD"),
    ("ETH-USD_UM-261225", "ETH-USD"),
    ("ETH-USD_UM-270326", "ETH-USD"),
    ("BTC-USD-261225", "BTC-USD"),
):
    mk = get(f"https://my.okx.com/api/v5/public/mark-price?instType=FUTURES&instId={inst}")
    ix = get(f"https://my.okx.com/api/v5/market/index-tickers?instId={idx}")
    ins = get(f"https://my.okx.com/api/v5/public/instruments?instType=FUTURES&instId={inst}")
    if mk and mk.get("data") and ix and ix.get("data") and ins and ins.get("data"):
        exp = dt.datetime.fromtimestamp(int(ins["data"][0]["expTime"]) / 1000, dt.timezone.utc)
        a, d = ann(float(mk["data"][0]["markPx"]), float(ix["data"][0]["idxPx"]), exp)
        print(f"okx     {inst} {a:6.2f}%  ({d:.0f}d)")
# perp premium for reference
for s in ("BTC", "ETH"):
    b = bn.get(s + "USDT")
    print(
        s,
        "binance perp mark/index bps",
        round((float(b["markPrice"]) / float(b["indexPrice"]) - 1) * 1e4, 2),
    )
