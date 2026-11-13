"""AR-7 P0 desk study: dated-futures basis lock (buy spot, short the quarterly, hold to
expiry), with the swap-like arm AR-8 as a descriptive extra. Test 2 of
`docs/strategies/research/arbitrage.md` section 6. IS ONLY by default (R37).

    python -m ntx.experiments.ar7_prototype
    python -m ntx.experiments.ar7_prototype --oos --theta=0.05   # ONCE; theta = the IS candidate

Free data only: Binance UM quarterlies (`BTCUSDT_YYMMDD`, `ETHUSDT_YYMMDD`) 1h klines,
Binance COIN-M `BTCUSD_YYMMDD` 1h klines, Binance spot 1h klines (all downloaded on first
run, checksum-verified, into data/raw/), perp 1h from the 1m dumps on disk, funding on disk.
Nothing live, no keys.

DECLARATION (arbitrage.md section 6) AND THE CLARIFICATIONS MADE BEFORE THE FIRST RUN
(each is the most conservative reading; none changes a number the declaration states):
  rule       first hourly bar whose close gives b = (F/S - 1) * 365 / days_to_expiry >= theta,
             theta in {5%, 8%}, days_to_expiry >= 30 (expiry = 08:00 UTC of the expiry date):
             buy spot, short the quarterly 1:1, hold to expiry. One entry per contract.
  timing     bars are indexed by CLOSE time (R8); entry fills at that bar's close.
  windows    A contract belongs to a window by its EXPIRY, so no IS contract settles in
             OOS and no 2024 contract is entered on 2023 information:
             IS  = entry >= 2021-03-01 and expiry <= 2023-12-31. 210326 can therefore never
                   enter (under 30 days left on 2021-03-01); it is listed as skipped.
             OOS = entry >= 2024-01-01 and expiry <= 2026-09-30.
             "2023 cycle" = a cycle whose EXPIRY falls in 2023 (230331..231229).
  settlement the exit price of the short leg is the quarterly's LAST hourly close; spot is
             sold at the spot close of the same hour. The residual basis F_T - S_T is
             therefore charged (or credited) as it was, not assumed zero.
  costs      percent of spot notional, ALL AT 2x as declared. Primary (gates) = TAKER:
             spot 10 + futures 5 at entry, spot 10 at exit, 5 settlement, 3 fills x 5 slippage
             = 45 bps at 1x, 90 at 2x. MAKER (reported): spot 8 + fut 2 entry, spot 8 exit,
             5 settlement, 15 slippage = 38 bps at 1x, 76 at 2x. Slippage is doubled too.
  L*         adverse excursion = max(hourly HIGH of the quarterly after entry) / F_entry - 1.
             L* = largest L in {1,2,3} with excursion * L <= 0.9 (L* = 0 if none). Spot gains
             sit in another account and are NOT netted (the FC-3 kill test). Posted capital
             = 1 (spot) + 1/L* (margin), per cycle.
  benchmark  excess = net return on posted capital - 4% * days_held / 365, per cycle.
             "2023 excess > 0" is read as: the MEAN per-cycle excess over 2023 cycles > 0
             (an empty 2023 set fails).
  candidate  theta = the one with the higher mean 2023 excess. IS PASSES iff that candidate
             passes all of: (1) >= 12 entered IS cycles (BTC+ETH); (2) every entered 2023
             cycle net-positive at taker 2x AND mean 2023 excess > 0; (3) L* >= 2 on every
             IS cycle. OOS (once, only if IS passes, only that theta): >= 8 entered cycles,
             every cycle net-positive at taker 2x, mean excess > 0, L* >= 2 on every cycle.
  controls   unconditional entry 60 days before every expiry (same costs), reported only.
  arm B      AR-8, DESCRIPTIVE ONLY, cannot pass a gate: on the arm-A entry bars, (B1) short
             quarterly + long perp (pays funding) and (B2) the reverse (receives funding),
             funding on disk applied at each settlement held through (R7); price legs on
             1h closes; costs 5 bps x 3 fills + 5 settlement + 5 slippage x 3 fills = 35,
             70 at 2x. BTC and ETH.
  arm C      COIN-M inverse BTCUSD_ quarterlies, DESCRIPTIVE ONLY, separate pool (expiry
             200925..231229, entry >= 2020-01-01). 1 BTC held as spot AND margin, short
             F_entry USD of contract: USD return = (F_entry/S_entry) * (S_T/F_T) - 1, same
             costs as arm A, no L* check.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from ntx.backtest.manifest import record
from ntx.data.binance_dumps import download
from ntx.experiments.sa1_prototype import funding, hourly

RAW = Path(__file__).resolve().parents[3] / "data" / "raw"
SITE = "https://data.binance.vision/data"

IS_START, IS_END = "2021-03-01", "2023-12-31"
OOS_START, OOS_END = "2024-01-01", "2026-09-30"
THETAS = (0.05, 0.08)
MIN_DAYS = 30
CONTROL_DAYS = 60
BENCH = 0.04
MAX_GAP_H = 6

# Quarterly expiries from the S3 listing (2026-10-28); 08:00 UTC delivery.
EXPIRIES_UM = [
    "210326",
    "210625",
    "210924",
    "211231",
    "220325",
    "220624",
    "220930",
    "221230",
    "230331",
    "230630",
    "230929",
    "231229",
    "240329",
    "240628",
    "240927",
    "241227",
    "250328",
    "250627",
    "250926",
    "251226",
    "260327",
    "260626",
    "260925",
]
EXPIRIES_CM = ("200925 201225 " + " ".join(EXPIRIES_UM[:12])).split()

# fractions of spot notional, 1x
COST_TAKER = (10 + 5 + 10 + 5 + 3 * 5) / 1e4
COST_MAKER = (8 + 2 + 8 + 5 + 3 * 5) / 1e4
COST_B = (3 * 5 + 5 + 3 * 5) / 1e4


def expiry_ts(code: str) -> pd.Timestamp:
    return pd.Timestamp(f"20{code[:2]}-{code[2:4]}-{code[4:]} 08:00", tz="UTC")


# ---------------------------------------------------------------- pure accounting


def basis_ann(f: float, s: float, days: float) -> float:
    return (f / s - 1.0) * 365.0 / days


def cycle_pnl(s0: float, f0: float, s_t: float, f_t: float) -> float:
    """Gross P&L of long spot / short future, per unit of ENTRY spot notional, no costs:
    (F0 - F_T)/S0 + (S_T - S0)/S0  ==  (F0 - S0)/S0 - (F_T - S_T)/S0."""
    for v in (s0, f0, s_t, f_t):
        if not v > 0:
            raise ValueError("non-positive price (R18)")
    return (f0 - f_t) / s0 + (s_t - s0) / s0


def survival_leverage(f0: float, highs: np.ndarray) -> tuple[int, float]:
    """Largest L in {1,2,3} with excursion * L <= 0.9, and the excursion itself."""
    exc = max(float(np.max(highs)) / f0 - 1.0, 0.0) if len(highs) else 0.0
    best = 0
    for lev in (1, 2, 3):
        if exc * lev <= 0.9:
            best = lev
    return best, exc


def first_entry(
    close_times: pd.DatetimeIndex,
    f: np.ndarray,
    s: np.ndarray,
    expiry: pd.Timestamp,
    theta: float,
    start: pd.Timestamp,
) -> int | None:
    for i, t in enumerate(close_times):
        if t < start:
            continue
        days = (expiry - t).total_seconds() / 86400.0
        if days < MIN_DAYS:
            return None
        if basis_ann(f[i], s[i], days) >= theta:
            return i
    return None


def control_entry(
    close_times: pd.DatetimeIndex, expiry: pd.Timestamp, start: pd.Timestamp
) -> int | None:
    target = expiry - pd.Timedelta(days=CONTROL_DAYS)
    if target < start:
        return None
    pos = int(close_times.searchsorted(target))
    return pos if pos < len(close_times) else None


# ---------------------------------------------------------------- data


def _read_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), header=None, dtype=str)
    return df[df[0].str.isdigit()]


def klines(url_dir: str, cache: str, name: str, first: str, last: str) -> pd.DataFrame | None:
    """1h klines for one instrument, indexed by CLOSE time (R8); columns high, close."""
    frames = []
    for month in pd.period_range(first, last, freq="M"):
        fname = f"{name}-1h-{month}.zip"
        path = download(f"{SITE}/{url_dir}/{name}/1h/{fname}", RAW / cache / name / fname)
        if path is not None:
            frames.append(_read_zip(path))
    if not frames:
        return None
    df = pd.concat(frames)
    t = df[0].astype("int64").values
    t = np.where(t > 10**14, t // 1000, t)  # spot dumps moved to microseconds in 2025
    idx = pd.to_datetime(t, unit="ms", utc=True) + pd.Timedelta(hours=1)
    out = pd.DataFrame(
        {"high": df[2].astype(float).values, "close": df[4].astype(float).values}, index=idx
    )
    return out[~out.index.duplicated()].sort_index()


def spot_series(symbol: str, first: str, last: str) -> pd.DataFrame:
    df = klines("spot/monthly/klines", "spot_klines", symbol, first, last)
    if df is None or df.empty:
        raise RuntimeError(f"{symbol}: no spot data (R35)")
    return df


def contract_series(kind: str, base: str, code: str) -> pd.DataFrame | None:
    exp = expiry_ts(code)
    first = (exp - pd.DateOffset(months=7)).strftime("%Y-%m")
    last = exp.strftime("%Y-%m")
    name = f"{base}_{code}"
    return klines(f"futures/{kind}/monthly/klines", f"delivery_{kind}", name, first, last)


# ---------------------------------------------------------------- one cycle


def run_cycle(
    fut: pd.DataFrame,
    spot: pd.DataFrame,
    expiry: pd.Timestamp,
    i: int,
    inverse: bool = False,
) -> dict:
    """Net cycle record from entry row `i` of the aligned frame to the last bar."""
    f, s = fut["close"].values, spot["close"].values
    n_last = len(fut) - 1
    s0, f0, s_t, f_t = s[i], f[i], s[n_last], f[n_last]
    gross = (f0 / s0) * (s_t / f_t) - 1.0 if inverse else cycle_pnl(s0, f0, s_t, f_t)
    days = (fut.index[n_last] - fut.index[i]).total_seconds() / 86400.0
    lev, exc = survival_leverage(f0, fut["high"].values[i + 1 :])
    net_t = gross - 2 * COST_TAKER
    net_m = gross - 2 * COST_MAKER
    cap = (1.0 + 1.0 / lev) if lev else float("nan")
    ret_cap = net_t / cap if lev else float("nan")
    return {
        "entry": str(fut.index[i]),
        "expiry": str(expiry),
        "year_expiry": int(expiry.year),
        "days_held": days,
        "basis_ann_entry": basis_ann(f0, s0, (expiry - fut.index[i]).total_seconds() / 86400.0),
        "gross_bps": gross * 1e4,
        "net_taker2x_bps": net_t * 1e4,
        "net_maker2x_bps": net_m * 1e4,
        "L_star": lev,
        "excursion": exc,
        "liq_at_3x": bool(exc * 3 > 0.9),
        "ret_on_capital": ret_cap,
        "excess": ret_cap - BENCH * days / 365.0 if lev else float("nan"),
        "idx": i,
    }


def align(fut: pd.DataFrame, spot: pd.DataFrame, expiry: pd.Timestamp):
    """Inner-join on close time; reject incomplete series. Returns (fut, spot) or reason."""
    both = fut.index.intersection(spot.index)
    both = both[both <= expiry]
    if len(both) < 24 * 5:
        return "too few bars"
    fut, spot = fut.loc[both], spot.loc[both]
    gaps = np.diff(both.values).astype("timedelta64[h]").astype(int)
    if gaps.max() > MAX_GAP_H:
        return f"gap of {gaps.max()} h"
    if (expiry - both[-1]) > pd.Timedelta(hours=24):
        return "last bar more than 24 h before expiry"
    return fut, spot


def study(window: tuple[str, str], theta: float | None, control: bool = False) -> dict:
    """Arm A over BTC+ETH for the window. theta None + control = unconditional-60d entry."""
    start, end = pd.Timestamp(window[0], tz="UTC"), pd.Timestamp(window[1], tz="UTC")
    cycles, skipped = [], []
    spots = {sym: spot_series(sym, "2020-12", "2026-09") for sym in ("BTCUSDT", "ETHUSDT")}
    for sym in ("BTCUSDT", "ETHUSDT"):
        for code in EXPIRIES_UM:
            exp = expiry_ts(code)
            if exp > end + pd.Timedelta(days=1) or exp < start:
                continue
            tag = f"{sym}_{code}"
            fut = contract_series("um", sym, code)
            if fut is None:
                skipped.append((tag, "no data"))
                continue
            al = align(fut, spots[sym], exp)
            if isinstance(al, str):
                skipped.append((tag, al))
                continue
            fut, spot = al
            if control:
                i = control_entry(fut.index, exp, start)
            else:
                i = first_entry(fut.index, fut["close"].values, spot["close"].values, exp,
                                theta, start)  # fmt: skip
            if i is None:
                skipped.append((tag, "no entry (threshold/30d/window)"))
                continue
            c = run_cycle(fut, spot, exp, i)
            c["symbol"], c["contract"] = sym, code
            c["_fut"], c["_spot"] = fut, spot
            cycles.append(c)
    return {"cycles": cycles, "skipped": skipped}


def summarise(cycles: list[dict]) -> dict:
    if not cycles:
        return {"n": 0}
    g = lambda k: np.array([c[k] for c in cycles], dtype=float)  # noqa: E731
    return {
        "n": len(cycles),
        "mean_gross_bps": float(g("gross_bps").mean()),
        "mean_net_taker2x_bps": float(g("net_taker2x_bps").mean()),
        "mean_net_maker2x_bps": float(g("net_maker2x_bps").mean()),
        "min_net_taker2x_bps": float(g("net_taker2x_bps").min()),
        "mean_basis_ann_entry": float(g("basis_ann_entry").mean()),
        "mean_days": float(g("days_held").mean()),
        "mean_ret_on_capital": float(np.nanmean(g("ret_on_capital"))),
        "mean_excess": float(np.nanmean(g("excess"))),
        "min_L_star": int(g("L_star").min()),
        "max_excursion": float(g("excursion").max()),
        "n_liq_at_3x": int(sum(c["liq_at_3x"] for c in cycles)),
        "all_net_positive": bool(all(c["net_taker2x_bps"] > 0 for c in cycles)),
    }


def gate(cycles: list[dict], min_cycles: int, oos: bool) -> dict:
    sub23 = [c for c in cycles if c["year_expiry"] == 2023]
    s_all, s23 = summarise(cycles), summarise(sub23)
    g1 = len(cycles) >= min_cycles
    if oos:
        g2 = bool(cycles) and s_all["all_net_positive"] and s_all["mean_excess"] > 0
    else:
        g2 = bool(sub23) and s23["all_net_positive"] and s23["mean_excess"] > 0
    g3 = bool(cycles) and s_all["min_L_star"] >= 2
    return {"g1_cycles": g1, "g2_net_and_excess": g2, "g3_Lstar": g3,
            "passes": bool(g1 and g2 and g3), "all": s_all, "y2023": s23}  # fmt: skip


# ---------------------------------------------------------------- arm B (descriptive)


def arm_b(cycles: list[dict]) -> dict:
    rows = []
    perp, fund = {}, {}
    for sym in ("BTCUSDT", "ETHUSDT"):
        perp[sym] = hourly(sym, "2021-01-01", "2023-12-31")
        fund[sym] = funding(sym, "2021-01-01", "2023-12-31")
    for c in cycles:
        sym = c["symbol"]
        fut = c["_fut"]
        t0, t1 = fut.index[c["idx"]], fut.index[-1]
        p = perp[sym]
        if t0 not in p.index or t1 not in p.index:
            continue
        f0, ft = fut["close"].values[c["idx"]], fut["close"].values[-1]
        p0, pt = p.loc[t0], p.loc[t1]
        fsum = float(fund[sym].loc[(fund[sym].index > t0) & (fund[sym].index <= t1)].sum())
        short_q = (f0 - ft) / f0
        long_perp = (pt - p0) / p0
        b1 = short_q + long_perp - fsum - 2 * COST_B  # long perp pays funding
        b2 = -(short_q + long_perp) + fsum - 2 * COST_B
        rows.append({"contract": f"{sym}_{c['contract']}", "funding_sum_bps": fsum * 1e4,
                     "B1_net_bps": b1 * 1e4, "B2_net_bps": b2 * 1e4})  # fmt: skip
    if not rows:
        return {"n": 0}
    a = lambda k: np.array([r[k] for r in rows])  # noqa: E731
    return {
        "n": len(rows),
        "B1_mean_net_2x_bps": float(a("B1_net_bps").mean()),
        "B2_mean_net_2x_bps": float(a("B2_net_bps").mean()),
        "share_B1_funding_paid": float((a("funding_sum_bps") > 0).mean()),
        "rows": rows,
    }


def arm_c(theta: float) -> dict:
    """COIN-M inverse BTCUSD_ pool, descriptive."""
    start = pd.Timestamp("2020-01-01", tz="UTC")
    spot = spot_series("BTCUSDT", "2020-01", "2024-01")
    cycles, skipped = [], []
    for code in EXPIRIES_CM:
        exp = expiry_ts(code)
        fut = contract_series("cm", "BTCUSD", code)
        if fut is None:
            skipped.append((code, "no data"))
            continue
        al = align(fut, spot, exp)
        if isinstance(al, str):
            skipped.append((code, al))
            continue
        fut, sp = al
        i = first_entry(fut.index, fut["close"].values, sp["close"].values, exp, theta, start)
        if i is None:
            skipped.append((code, "no entry"))
            continue
        cycles.append(run_cycle(fut, sp, exp, i, inverse=True) | {"contract": code})
    return {"cycles": cycles, "skipped": skipped}


# ---------------------------------------------------------------- reporting


def clean(cycles: list[dict]) -> list[dict]:
    return [{k: v for k, v in c.items() if not k.startswith("_")} for c in cycles]


def fmt_summary(tag: str, s: dict) -> str:
    if s.get("n", 0) == 0:
        return f"{tag}: n=0"
    return (
        f"{tag}: n={s['n']} gross={s['mean_gross_bps']:.1f} netT2x={s['mean_net_taker2x_bps']:.1f}"
        f" netM2x={s['mean_net_maker2x_bps']:.1f} minNetT2x={s['min_net_taker2x_bps']:.1f}"
        f" basis_ann={s['mean_basis_ann_entry']:.4f} days={s['mean_days']:.0f}"
        f" retCap={s['mean_ret_on_capital']:.4f} excess={s['mean_excess']:.4f}"
        f" minL*={s['min_L_star']} maxExc={s['max_excursion']:.3f} liq3x={s['n_liq_at_3x']}"
    )


def main() -> int:
    oos = "--oos" in sys.argv
    window = (OOS_START, OOS_END) if oos else (IS_START, IS_END)
    lines = [f"AR-7 P0 basis lock, {'OOS' if oos else 'IS'} {window[0]}..{window[1]}, BTC+ETH"]
    results: dict = {}
    thetas = THETAS
    if oos:
        # the single declared candidate is passed in by hand from the recorded IS run
        arg = [a for a in sys.argv if a.startswith("--theta=")]
        if not arg:
            raise SystemExit("--oos needs --theta=<IS candidate> (from the IS verdict)")
        thetas = (float(arg[0].split("=")[1]),)
    for th in thetas:
        res = study(window, th)
        g = gate(res["cycles"], 8 if oos else 12, oos)
        results[th] = {"gate": g, "cycles": clean(res["cycles"]), "skipped": res["skipped"]}
        lines.append(f"-- theta={th:.0%}  entered={len(res['cycles'])}  "
                     f"skipped={len(res['skipped'])}")  # fmt: skip
        lines.append(fmt_summary("  all ", g["all"]))
        lines.append(fmt_summary("  2023", g["y2023"]))
        lines.append(f"  gates: (1) cycles {g['g1_cycles']}  (2) net+excess {g['g2_net_and_excess']}"
                     f"  (3) L*>=2 {g['g3_Lstar']}  -> {'PASS' if g['passes'] else 'FAIL'}")  # fmt: skip
        for c in clean(res["cycles"]):
            lines.append(
                f"    {c['symbol'][:3]}_{c['contract']} in {c['entry'][:10]} basis="
                f"{c['basis_ann_entry']:.3f} d={c['days_held']:.0f} gross={c['gross_bps']:.0f} "
                f"netT2x={c['net_taker2x_bps']:.0f} L*={c['L_star']} exc={c['excursion']:.3f} "
                f"ex={c['excess']:.4f}"
            )
        for tag, why in res["skipped"]:
            lines.append(f"    skipped {tag}: {why}")
    ctl = study(window, None, control=True)
    results["control"] = {"summary": summarise(ctl["cycles"]), "skipped": ctl["skipped"]}
    lines.append(fmt_summary("control (unconditional 60d)", results["control"]["summary"]))

    verdict, candidate = "n/a", None
    if not oos:
        ex23 = {
            th: (results[th]["gate"]["y2023"].get("mean_excess", float("-inf"))) for th in THETAS
        }
        candidate = max(ex23, key=lambda k: ex23[k])
        lines.append(f"2023 mean excess by theta: {ex23} -> candidate theta={candidate:.0%}")
        verdict = "PASS" if results[candidate]["gate"]["passes"] else "FAIL"
        # descriptive arms, on the candidate's entries
        results["arm_B"] = arm_b(study(window, candidate)["cycles"])
        b = results["arm_B"]
        lines.append(f"arm B (descriptive, cannot gate): n={b.get('n')} "
                     f"B1(short Q/long perp) mean net2x={b.get('B1_mean_net_2x_bps', 0):.1f} bps  "
                     f"B2(reverse)={b.get('B2_mean_net_2x_bps', 0):.1f} bps  "
                     f"share funding paid by B1={b.get('share_B1_funding_paid', 0):.2f}")  # fmt: skip
        c = arm_c(candidate)
        results["arm_C"] = {"summary": summarise(c["cycles"]), "skipped": c["skipped"]}
        lines.append(
            fmt_summary("arm C (inverse COIN-M BTC, descriptive)", results["arm_C"]["summary"])
        )
    else:
        verdict = "PASS" if results[thetas[0]]["gate"]["passes"] else "FAIL"
    lines.append(f"AR-7 P0 VERDICT ({'OOS' if oos else 'IS'}): {verdict}"
                 + (f"  candidate theta={candidate:.0%}" if candidate else ""))  # fmt: skip
    out = "\n".join(lines)
    print(out)
    path = record(
        label="ar7-p0-OOS" if oos else "ar7-p0-IS",
        spec={
            "window": window,
            "thetas": thetas,
            "min_days": MIN_DAYS,
            "cost_taker_1x": COST_TAKER,
            "cost_maker_1x": COST_MAKER,
            "benchmark_annual": BENCH,
            "expiries_um": EXPIRIES_UM,
        },
        metrics=json.loads(json.dumps({"verdict": verdict, "candidate": candidate,
                                       "results": {str(k): v for k, v in results.items()}},
                                      default=lambda o: o.item() if hasattr(o, "item") else str(o))),
        extra={"script": "ntx.experiments.ar7_prototype", "rung": "P0"},
        stdout=out,
    )  # fmt: skip
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
