"""S008 mean-reversion study. IN-SAMPLE ONLY (R37). Funding applied (R7). Recorded (R42).

    python -m ntx.experiments.mean_reversion_study calibrate   # measured inputs, no backtest
    python -m ntx.experiments.mean_reversion_study smoke       # one cell, to time the grid
    python -m ntx.experiments.mean_reversion_study grid        # the declared 3x3 sweep
    python -m ntx.experiments.mean_reversion_study confirm     # winner at 4h + 2x cost stress
    python -m ntx.experiments.mean_reversion_study correlate   # vs the S003 live-config IS curve

`OOS_WINDOW` is deliberately not imported. Every window here is
`IS_WINDOW = 2020-01-01..2023-12-31`. The out-of-sample window is touched once,
for one chosen candidate, by whoever owns the gate -- not here (R37, F017).

THE DECLARED GRID, fixed before the first cell ran:

    lookback  in {24, 48, 96}     bars of prior closes (1, 2, 4 days at 1h)
    entry_z   in {1.5, 2.0, 2.5}  sigmas of dislocation required

and nothing else. Everything below is held constant across all nine cells:

    timeframe 1h, atr_period 56, sl_atr_mult from `calibrate` (see SL_ATR_MULT),
    tsl 1.5%/1.0%, max_bars_in_position 48, risk 0.25%, 6 concurrent positions,
    min_sigma_price_pct 0.05, and the live account's equity/assets/slippage.

Any cell run outside this grid is reported as such, with the reason.

ARM 2 (`exitfix`) IS POST-HOC. It was declared only after all nine cells of the
grid above had run, and it exists because those nine agreed on a diagnosis: the
signal produces a 60.5% win rate -- genuine mean-reversion behaviour -- while the
payoff ratio is 0.46, so every cell loses. The cause is structural and is visible
without any further fitting: the exit stack was inherited wholesale from S003,
where `tsl_activation_price_pct=1.5` / `tsl_trail_price_pct=1.0` sits against a
4h trend with a 31-hour mean hold. On a mean-reversion trade it caps a win near
+0.5% of price while the 4 x ATR stop lets a loss run to -4%. Break-even at a
60.5% win rate needs a payoff of 0.65.

Arm 2 therefore replaces the trailing stop with the exit the thesis actually
implies -- take profit at roughly the size of the dislocation being faded, with
the ATR stop and the time stop underneath -- and sweeps the two numbers that
define it. It is reported as a second grid, not folded into the first, because a
result found after looking is worth less than a result found before, and
pretending otherwise is how a losing strategy gets deployed.
"""

from __future__ import annotations

import argparse
import math
import time

import pandas as pd

from ntx.backtest import funding as fnd
from ntx.backtest.runner import BacktestSpec
from ntx.data.read import open_catalog, read_bars
from ntx.experiments._common import DATA, IS_WINDOW, Outcome, evaluate, record
from ntx.experiments.s003_gate import LIVE_ASSETS, LIVE_EQUITY_USD, LIVE_SLIPPAGE_BPS
from ntx.strategies.donchian_breakout import DonchianBreakoutConfig, DonchianBreakoutStrategy
from ntx.strategies.mean_reversion import MeanReversionConfig, MeanReversionStrategy
from ntx.strategies.registry import DEPLOYED

IS_DAYS = 1461  # 2020-01-01 .. 2023-12-31 inclusive

# ---------------------------------------------------------------------------- #
# Held constant across the grid.
# ---------------------------------------------------------------------------- #
TIMEFRAME = "1h"
ATR_PERIOD = 56  # same wall-clock horizon as S003's atr_period=14 on 4h bars

# A 1h bar's true range is far smaller than a 4h bar's, so lengthening the ATR
# period smooths it but does NOT restore its size -- and the stop's width as a
# fraction of price is the single biggest driver of fee drag
# (fee/gross ~= 0.0020 / (stop_frac * expectancy_R)). `calibrate` measures the
# ratio from the catalog instead of assuming it; this constant is the measured
# multiplier that puts the 1h stop at the same percentage of price as S003's
# 2.0 x ATR(14) on 4h.
SL_ATR_MULT = 4.0

FIXED = dict(
    atr_period=ATR_PERIOD,
    sl_atr_mult=SL_ATR_MULT,
    tsl_enabled=True,
    tsl_activation_price_pct=1.5,
    tsl_trail_price_pct=1.0,
    # The only exit that expresses "this did not revert": a hard bound on the
    # hold. A mean-reversion trade that is still open two days later is not a
    # rubber band, it is a trend going the wrong way. See the module docstring
    # in `strategies/mean_reversion.py` for why there is no "close at the mean".
    max_bars_in_position=48,
    min_sigma_price_pct=0.05,
    # Measured by the scout on S003: at unchanged aggregate risk
    # (risk x positions), 6 positions at 0.25% strictly beats 3 at 0.5% on
    # Sharpe, PF, drawdown AND trade count, without tripping the 20% KILL.
    risk_equity_pct=0.25,
    max_concurrent_positions=6,
    max_leverage=3.0,
)

LOOKBACKS = (24, 48, 96)
ENTRY_ZS = (1.5, 2.0, 2.5)

# ARM 2, declared before it was run but AFTER arm 1's nine cells were read.
# The trailing stop is switched off and replaced by a fixed take profit sized to
# the reversion being faded; `sl_atr_mult` is swept because the stop's width is
# both the left-tail protection and the dominant term in fee drag.
EXITFIX_TP_PCT = (1.0, 2.0)
EXITFIX_SL_MULT = (1.0, 2.0)
EXITFIX = dict(tsl_enabled=False, tsl_activation_price_pct=1.5, tsl_trail_price_pct=1.0)

HEADER = (
    f"{'lookback':>9}{'z':>6}{'trades':>8}{'ent/day':>9}{'Sharpe':>8}{'PF+fund':>9}"
    f"{'maxDD%':>8}{'mtm%':>8}{'win%':>7}{'fee/gr%':>9}{'fund%':>8}{'killed':>8}"
)


# Diagnostic only. `max_drawdown_equity_pct` defaults to 20 and, once breached,
# `_kill_all` latches `self._killed = True` for the rest of the run -- every
# later entry is refused. The first cell run here was killed on 2020-03-12 (the
# COVID crash) and the remaining 3.8 years are a flat line, so a guard-on grid
# would rank cells by "how fast did it die", not by whether the rule has edge.
# Selecting on the guards-off arm and then RE-RUNNING the winner with the guards
# back on is the only way to see both. A guards-off number is never a result:
# every label carries `-nokill`.
NO_GUARDS = dict(max_drawdown_equity_pct=0.0, daily_loss_limit_equity_pct=0.0)


def spec_for(lookback: int, entry_z: float, **over) -> BacktestSpec:
    """The one place a spec is built, so no cell can differ by accident."""
    kwargs = dict(FIXED, lookback=lookback, entry_z=entry_z)
    if over.get("no_guards"):
        kwargs.update(NO_GUARDS)
    kwargs.update(
        {k: v for k, v in over.items() if k not in ("timeframe", "fee_multiplier", "no_guards")}
    )
    return BacktestSpec(
        strategy_cls=MeanReversionStrategy,
        config_cls=MeanReversionConfig,
        config_kwargs=kwargs,
        timeframe=over.get("timeframe", TIMEFRAME),
        start=IS_WINDOW[0],
        end=IS_WINDOW[1],
        starting_equity_usd=LIVE_EQUITY_USD,
        slippage_bps_per_side=LIVE_SLIPPAGE_BPS,
        assets=LIVE_ASSETS,
        fee_multiplier=over.get("fee_multiplier", 1.0),
    )


def fee_share_pct(out: Outcome) -> float:
    """Fees as a share of gross profit, the project's definition (sweep.py:44).

    Meaningless when gross is negative -- the denominator collapses and the
    ratio explodes -- so a losing run must be read as dollars, not as this.
    """
    m = out.metrics
    gross = m.net_pnl + m.fees_paid
    return (m.fees_paid / abs(gross) * 100.0) if gross else float("nan")


def row(lookback: int, entry_z: float, out: Outcome) -> str:
    m = out.metrics
    killed = out.counters.get("entry_skips", {}).get("killed", 0)
    return (
        f"{lookback:>9}{entry_z:>6.1f}{m.trades:>8}{m.trades / IS_DAYS:>9.2f}"
        f"{m.sharpe:>8.2f}{out.pf_after_funding:>9.2f}{m.max_drawdown_pct:>8.1f}"
        f"{m.return_pct:>8.0f}{m.win_rate * 100:>7.1f}{fee_share_pct(out):>9.0f}"
        f"{out.funding_impact.pct_of_gross_pnl:>8.1f}{killed:>8}"
    )


def detail(out: Outcome) -> str:
    c = out.counters
    return (
        f"    counters: attempted={c.get('entries_attempted')} "
        f"submitted={c.get('entries_submitted')} rejects={c.get('rejects')} "
        f"entry_skips={c.get('entry_skips')} sigma_floor_vetoes={c.get('filtered_out')}\n"
        f"    exits={c.get('exits')} size_caps={c.get('size_caps')}\n"
        f"    fees=${out.metrics.fees_paid:,.2f} exposure={out.metrics.exposure_pct:.1f}% "
        f"unrealised_at_end={out.metrics.unrealized_at_end_pct:.1f}%\n"
        f"    red_flags={out.metrics.red_flags()}"
    )


# ---------------------------------------------------------------------------- #
# calibrate -- measured design inputs, no backtest, no result claimed
# ---------------------------------------------------------------------------- #
def _atr_pct(bars: list, period: int) -> float:
    """Mean ATR as a percentage of close, Wilder-smoothed, over a bar list."""
    df = pd.DataFrame(
        {
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
        }
    )
    prev = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    return float((atr / df["close"] * 100.0).iloc[period:].mean())


def calibrate() -> int:
    """Two measurements that fix constants in FIXED, so neither is folklore (R11).

    1. How wide 2 x ATR(14) on 4h is as a share of price, versus ATR(56) on 1h.
       That ratio sets SL_ATR_MULT, and the stop's width is the dominant term in
       fee drag.
    2. How often the rolling sigma is below the 0.05% floor, so the guard is
       shown to be live rather than decorative (R40).
    """
    cat = open_catalog(DATA)
    print(f"ATR as % of price, {IS_WINDOW[0]}..{IS_WINDOW[1]}\n")
    print(f"{'asset':>6}{'4h ATR14 %':>13}{'1h ATR56 %':>13}{'ratio':>8}")
    print("-" * 40)
    ratios = []
    sigma_rows = []
    for base in LIVE_ASSETS:
        inst = f"{base}-USDT-SWAP.OKX"
        b4 = read_bars(cat, inst, "4h", start=IS_WINDOW[0], end=IS_WINDOW[1])
        b1 = read_bars(cat, inst, "1h", start=IS_WINDOW[0], end=IS_WINDOW[1])
        if not b4 or not b1:
            raise RuntimeError(f"no bars for {inst}; the catalog is incomplete (R35)")
        a4, a1 = _atr_pct(b4, 14), _atr_pct(b1, 56)
        ratios.append(a4 / a1)
        print(f"{base:>6}{a4:>13.3f}{a1:>13.3f}{a4 / a1:>8.2f}")

        closes = pd.Series([float(b.close) for b in b1])
        for lb in LOOKBACKS:
            sigma = closes.rolling(lb).std(ddof=0).shift(1)
            frac = (sigma / closes * 100.0).dropna()
            sigma_rows.append((base, lb, float((frac < 0.05).mean() * 100.0), float(frac.median())))

    ratio = sum(ratios) / len(ratios)
    print(f"\nmean 4h/1h ratio = {ratio:.2f}")
    print(
        f"S003 stop = 2.0 x ATR14(4h); the same width on 1h needs "
        f"sl_atr_mult = 2.0 x {ratio:.2f} = {2.0 * ratio:.2f}  (FIXED uses {SL_ATR_MULT})"
    )

    print("\nRolling sigma below the 0.05% floor (R40: is the guard live?)")
    print(f"{'lookback':>9}{'% of bars below floor':>24}{'median sigma/price %':>23}")
    print("-" * 56)
    sig = pd.DataFrame(sigma_rows, columns=["asset", "lookback", "below_pct", "median_pct"])
    for lb, g in sig.groupby("lookback"):
        print(f"{lb:>9}{g['below_pct'].mean():>24.2f}{g['median_pct'].median():>23.3f}")
    worst = sig.sort_values("below_pct", ascending=False).head(3)
    print("\nworst three (asset, lookback, % of bars below floor):")
    for _, r in worst.iterrows():
        print(f"    {r['asset']:<6} lb={int(r['lookback']):<4} {r['below_pct']:.2f}%")
    return 0


def edge() -> int:
    """Is the SIGNAL predictive, before any exit stack or cost touches it?

    The nine grid cells all lose, and a losing backtest has two very different
    explanations: the rule has no edge, or the rule has edge and the exits throw
    it away. This separates them. For every 1h bar in the IS window it computes
    the same prior-window z-score the strategy uses, then the raw forward return
    over several horizons -- no stop, no trailing stop, no fees, no funding, no
    position cap. If the conditional forward return after a dislocation is not
    reliably better than the unconditional one, no exit stack can rescue it and
    arm 2 is a waste of a cycle.

    This is a diagnostic on the in-sample window, not a result: it has no costs
    in it, so it can never be quoted as a return.
    """
    cat = open_catalog(DATA)
    horizons = (6, 12, 24, 48)
    rows = []
    for base in LIVE_ASSETS:
        bars = read_bars(
            cat, f"{base}-USDT-SWAP.OKX", TIMEFRAME, start=IS_WINDOW[0], end=IS_WINDOW[1]
        )
        if not bars:
            raise RuntimeError(f"no {TIMEFRAME} bars for {base} (R35)")
        close = pd.Series([float(b.close) for b in bars])
        for lb in LOOKBACKS:
            mean = close.rolling(lb).mean().shift(1)
            sigma = close.rolling(lb).std(ddof=0).shift(1)
            z = ((close - mean) / sigma).replace([float("inf"), float("-inf")], pd.NA)
            for h in horizons:
                fwd = close.shift(-h) / close - 1.0
                for zt in ENTRY_ZS:
                    for side, mask in (
                        ("long", z <= -zt),
                        ("short", z >= zt),
                    ):
                        sel = fwd[mask.fillna(False)].dropna()
                        if len(sel) < 30:
                            continue
                        # A short earns the negative of the forward return.
                        signed = sel if side == "long" else -sel
                        rows.append(
                            dict(
                                asset=base,
                                lookback=lb,
                                horizon=h,
                                z=zt,
                                side=side,
                                n=len(sel),
                                mean_bps=float(signed.mean() * 10_000),
                                hit=float((signed > 0).mean() * 100.0),
                            )
                        )
        for h in horizons:
            fwd = (close.shift(-h) / close - 1.0).dropna()
            rows.append(
                dict(
                    asset=base,
                    lookback=0,
                    horizon=h,
                    z=0.0,
                    side="unconditional",
                    n=len(fwd),
                    mean_bps=float(fwd.mean() * 10_000),
                    hit=float((fwd > 0).mean() * 100.0),
                )
            )

    df = pd.DataFrame(rows)
    print(f"Raw forward return after a dislocation, {TIMEFRAME} bars, IS only.")
    print("NO costs, NO stop, NO exits -- a diagnostic, never a result.\n")

    # The benchmark is NOT zero. Crypto drifted up over 2020-2023, so a long
    # signal inherits that drift for free and a short pays it. Only the EXCESS
    # over the unconditional forward return is attributable to the signal.
    unc = df[df.side == "unconditional"].groupby("horizon")["mean_bps"].mean()
    print("unconditional mean forward return across the ten (the benchmark):")
    for h in horizons:
        print(f"    {h:>3}h  {unc[h]:+8.1f} bps")

    d = df[df.side != "unconditional"]
    g = (
        d.groupby(["lookback", "z", "side", "horizon"])
        .agg(n=("n", "sum"), mean_bps=("mean_bps", "mean"), hit=("hit", "mean"))
        .reset_index()
    )
    # A short earns -fwd, so its benchmark is -unconditional.
    g["excess_bps"] = g.apply(
        lambda r: (
            r["mean_bps"] - (unc[r["horizon"]] if r["side"] == "long" else -unc[r["horizon"]])
        ),
        axis=1,
    )
    print(
        f"\n{'lookback':>9}{'z':>6}{'side':>7}{'horizon':>9}{'n':>9}"
        f"{'mean bps':>11}{'excess':>9}{'hit%':>8}{'beats 20bps':>13}"
    )
    print("-" * 81)
    for _, r in g.iterrows():
        print(
            f"{int(r['lookback']):>9}{r['z']:>6.1f}{r['side']:>7}{int(r['horizon']):>9}"
            f"{int(r['n']):>9}{r['mean_bps']:>11.1f}{r['excess_bps']:>9.1f}{r['hit']:>8.1f}"
            f"{('YES' if r['excess_bps'] > 20.0 else 'no'):>13}"
        )
    # The cost line every one of these must clear to be tradable at live config.
    print("\nround-trip cost at live config = 20 bps of notional (2 x (5 taker + 5 slip))")
    return 0


# ---------------------------------------------------------------------------- #
# backtest modes
# ---------------------------------------------------------------------------- #
def _run(label: str, lookback: int, entry_z: float, series, **over) -> Outcome:
    if over.get("no_guards"):
        label = f"{label}-nokill"
    spec = spec_for(lookback, entry_z, **over)
    t0 = time.time()
    out = evaluate(spec, series)
    path = record(
        label,
        spec,
        out,
        script=__name__,
        window=list(IS_WINDOW),
        declared_grid={"lookback": list(LOOKBACKS), "entry_z": list(ENTRY_ZS)},
        fee_share_pct=fee_share_pct(out),
        entries_per_day=out.metrics.trades / IS_DAYS,
        loss_guards_disabled=bool(over.get("no_guards")),
    )
    print(row(lookback, entry_z, out))
    print(detail(out))
    print(f"    -> {path.name}   ({time.time() - t0:.0f}s)")
    return out


def smoke(lookback: int, entry_z: float, no_guards: bool) -> int:
    """One cell, to time the grid and sanity-check the entry rate before nine."""
    series = fnd.load_funding_series(DATA / "raw")
    print(HEADER)
    print("-" * len(HEADER))
    _run(f"s008-mr-smoke-{lookback}-{entry_z}-IS", lookback, entry_z, series, no_guards=no_guards)
    return 0


def grid(no_guards: bool) -> int:
    series = fnd.load_funding_series(DATA / "raw")
    print(
        f"S008 mean reversion, {TIMEFRAME} bars, IS {IS_WINDOW[0]}..{IS_WINDOW[1]}, "
        f"${LIVE_EQUITY_USD:,.2f} on {len(LIVE_ASSETS)} assets, "
        f"{LIVE_SLIPPAGE_BPS:.0f}bps/side, funding applied\n"
        f"fixed: {FIXED}\n"
        + (
            "LOSS GUARDS DISABLED -- diagnostic arm, not a result. The guard-on\n"
            "re-run of the selected cell is the reportable number.\n"
            if no_guards
            else ""
        )
    )
    print(HEADER)
    print("-" * len(HEADER))
    for lookback in LOOKBACKS:
        for entry_z in ENTRY_ZS:
            _run(
                f"s008-mr-{lookback}-z{entry_z}-IS",
                lookback,
                entry_z,
                series,
                no_guards=no_guards,
            )
    return 0


def exitfix(lookback: int, entry_z: float, no_guards: bool) -> int:
    """ARM 2: the exit stack the thesis implies, instead of S003's.

    POST-HOC -- see the module docstring. Reported as its own grid.
    """
    series = fnd.load_funding_series(DATA / "raw")
    print(
        f"ARM 2 (POST-HOC): lookback={lookback} entry_z={entry_z} fixed from arm 1.\n"
        f"Trailing stop OFF; take profit sized to the dislocation; ATR stop swept.\n"
    )
    print(f"{'tp%':>6}{'slATR':>7}" + HEADER[15:])
    print("-" * len(HEADER))
    for tp in EXITFIX_TP_PCT:
        for sl in EXITFIX_SL_MULT:
            out = _run(
                f"s008-mr-exitfix-tp{tp}-sl{sl}-IS",
                lookback,
                entry_z,
                series,
                no_guards=no_guards,
                tp_price_pct=tp,
                sl_atr_mult=sl,
                **EXITFIX,
            )
            m = out.metrics
            print(
                f"    ^ tp={tp}% sl={sl}xATR  payoff={m.payoff_ratio:.2f} "
                f"avg_win=${m.avg_win:.4f} avg_loss=${m.avg_loss:.4f} "
                f"expectancy=${m.net_pnl / m.trades if m.trades else 0:.5f}/trade"
            )
    return 0


def confirm(lookback: int, entry_z: float, no_guards: bool) -> int:
    """Robustness of the chosen cell: a second timeframe and 2x costs.

    docs/09 §7 wants the edge visible at more than one timeframe and surviving a
    cost stress. Both are still IN-SAMPLE; neither is a substitute for the OOS
    gate, which is not run here (R37).
    """
    series = fnd.load_funding_series(DATA / "raw")
    print(HEADER)
    print("-" * len(HEADER))
    print("4h (same rule, half the bar count):")
    _run(
        f"s008-mr-{lookback}-z{entry_z}-4h-IS",
        lookback,
        entry_z,
        series,
        timeframe="4h",
        no_guards=no_guards,
    )
    print("\n2x fees and slippage (docs/09 §7 cost stress):")
    _run(
        f"s008-mr-{lookback}-z{entry_z}-cost2x-IS",
        lookback,
        entry_z,
        series,
        fee_multiplier=2.0,
        no_guards=no_guards,
    )
    return 0


def correlate(lookback: int, entry_z: float, no_guards: bool) -> int:
    """Daily-return correlation against the S003 live-config IS equity curve.

    A second strategy that wins when the incumbent wins is leverage, not
    diversification. This is the number that decides whether S008 is worth
    running alongside S003 rather than instead of it.
    """
    series = fnd.load_funding_series(DATA / "raw")
    print(HEADER)
    print("-" * len(HEADER))
    # The correlation must describe the RULE's return stream. With the guards on
    # this cell is killed on 2020-03-12 and the curve is a flat line for the
    # remaining 3.8 years, which would correlate with nothing at all.
    mr = _run(
        f"s008-mr-{lookback}-z{entry_z}-corr-IS",
        lookback,
        entry_z,
        series,
        no_guards=no_guards,
    )

    s003 = BacktestSpec(
        strategy_cls=DonchianBreakoutStrategy,
        config_cls=DonchianBreakoutConfig,
        config_kwargs=DEPLOYED["S003"],
        timeframe="4h",
        start=IS_WINDOW[0],
        end=IS_WINDOW[1],
        starting_equity_usd=LIVE_EQUITY_USD,
        slippage_bps_per_side=LIVE_SLIPPAGE_BPS,
        assets=LIVE_ASSETS,
    )
    inc = evaluate(s003, series)
    path = record("s008-corr-s003-baseline-IS", s003, inc, script=__name__, window=list(IS_WINDOW))
    print(f"\nS003 live-config IS baseline (for the correlation): {row(0, 0.0, inc)}")
    print(f"    -> {path.name}")

    # Both curves are sampled per bar, at different frequencies, so they are
    # resampled to a common daily grid before differencing. Using the last
    # observation of each UTC day keeps the comparison to closed bars only.
    def daily(equity: pd.Series) -> pd.Series:
        return equity.resample("1D").last().ffill().pct_change().dropna()

    a, b = daily(mr.equity), daily(inc.equity)
    joined = pd.concat({"s008": a, "s003": b}, axis=1).dropna()
    pearson = float(joined["s008"].corr(joined["s003"]))
    # Rank correlation computed as Pearson on ranks: pandas' method="spearman"
    # imports scipy, which this project does not depend on.
    spearman = float(joined["s008"].rank().corr(joined["s003"].rank()))
    both_up = float(((joined["s008"] > 0) & (joined["s003"] > 0)).mean() * 100.0)
    s008_saves = float(((joined["s008"] > 0) & (joined["s003"] < 0)).mean() * 100.0)

    # A 50/50 blend of the two daily return streams: does the pair Sharpe beat
    # the incumbent alone? That, not the correlation sign on its own, is the
    # reason to run two strategies.
    blend = (joined["s008"] + joined["s003"]) / 2.0
    ann = math.sqrt(365.0)

    def sharpe(x: pd.Series) -> float:
        return float(x.mean() / x.std() * ann) if x.std() > 0 else 0.0

    print(f"\ndaily-return correlation over {len(joined)} common days")
    print(f"    pearson              {pearson:+.3f}")
    print(f"    spearman             {spearman:+.3f}")
    print(f"    both up              {both_up:.1f}% of days")
    print(f"    S008 up, S003 down   {s008_saves:.1f}% of days")
    print(
        f"    daily Sharpe  S003 {sharpe(b):+.2f}  S008 {sharpe(a):+.2f}  50/50 {sharpe(blend):+.2f}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S008 mean-reversion study (in-sample only)")
    ap.add_argument(
        "mode",
        choices=(
            "calibrate",
            "edge",
            "smoke",
            "grid",
            "exitfix",
            "confirm",
            "correlate",
        ),
        nargs="?",
        default="grid",
    )
    ap.add_argument("--lookback", type=int, default=48)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument(
        "--no-guards",
        action="store_true",
        help="DIAGNOSTIC: disable the drawdown KILL and daily-loss PAUSE so the "
        "cells rank by edge rather than by how early they died. Labels get -nokill.",
    )
    a = ap.parse_args(argv)
    if a.mode == "calibrate":
        return calibrate()
    if a.mode == "edge":
        return edge()
    if a.mode == "smoke":
        return smoke(a.lookback, a.entry_z, a.no_guards)
    if a.mode == "grid":
        return grid(a.no_guards)
    if a.mode == "exitfix":
        return exitfix(a.lookback, a.entry_z, a.no_guards)
    if a.mode == "confirm":
        return confirm(a.lookback, a.entry_z, a.no_guards)
    return correlate(a.lookback, a.entry_z, a.no_guards)


if __name__ == "__main__":
    raise SystemExit(main())
