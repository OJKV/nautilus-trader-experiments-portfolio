"""Diagnostic: print historical and live bar timestamps from OKX. No orders.

Answers "what does ts_event mean for OKX bars, history vs websocket?" -- the
de-duplication in NtxStrategy depends on both using the same convention.

    NTX_VENUE=okx_demo NTX_EQUITY_OVERRIDE_USD=1 python -m ntx.ops.bar_probe
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.trading.strategy import Strategy


def _t(ns: int) -> str:
    return pd.Timestamp(ns, tz="UTC").strftime("%H:%M:%S.%f")[:-3]


class ProbeConfig(StrategyConfig, frozen=True):
    bar_type: str
    minutes_back: int = 5
    limit: int = 0


class BarProbe(Strategy):
    def on_start(self) -> None:
        bt = BarType.from_str(self.config.bar_type)
        self.subscribe_bars(bt)
        now = self.clock.utc_now()
        self.request_bars(
            bt,
            start=now - timedelta(minutes=self.config.minutes_back),
            end=now,
            limit=self.config.limit,
            callback=lambda rid: print(f"PROBE HIST DONE count={self.n}", flush=True),
        )
        self.n = 0

    def on_historical_data(self, data) -> None:
        if isinstance(data, Bar):
            self.n += 1
            print(
                f"PROBE HIST ts_event={_t(data.ts_event)} ts_init={_t(data.ts_init)} "
                f"close={data.close}",
                flush=True,
            )

    def on_bar(self, bar: Bar) -> None:
        print(
            f"PROBE LIVE ts_event={_t(bar.ts_event)} ts_init={_t(bar.ts_init)} "
            f"now={_t(self.clock.timestamp_ns())} close={bar.close}",
            flush=True,
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", default="1-MINUTE")
    ap.add_argument("--minutes-back", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    from nautilus_trader.adapters.okx.factories import (
        OKXLiveDataClientFactory,
        OKXLiveExecClientFactory,
    )
    from nautilus_trader.live.node import TradingNode

    from ntx.config.geo import check_egress
    from ntx.config.settings import load
    from ntx.live.node import VENUE_NAME, node_config, plan, resolve_instruments

    if not check_egress().allowed:
        print("REFUSING: egress not allowed")
        return 2
    settings = load()
    p = plan(settings, strategy_id="S900", instrument_ids=resolve_instruments("SOL"))
    node = TradingNode(config=node_config(p, settings))
    node.trader.add_strategy(
        BarProbe(
            ProbeConfig(
                bar_type=f"{p.instrument_ids[0]}-{args.bar}-LAST-EXTERNAL",
                minutes_back=args.minutes_back,
                limit=args.limit,
            )
        )
    )
    node.add_data_client_factory(VENUE_NAME, OKXLiveDataClientFactory)
    node.add_exec_client_factory(VENUE_NAME, OKXLiveExecClientFactory)
    node.build()
    try:
        node.run()
    finally:
        node.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
