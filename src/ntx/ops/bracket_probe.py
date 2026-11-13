"""DEMO-ONLY probe for gap G7: can the stop be attached to the entry order?

Phase 1: market BUY 0.01 SOL with an attached stop 0.08% below -> should trigger
         within minutes. Checks: accepted/fill events for the child, protect gap.
Phase 2: same with a far stop; then cancel the child through nautilus.
Everything is flattened at the end (post-run REST flatten of SOL).

    NTX_VENUE=okx_demo NTX_EQUITY_OVERRIDE_USD=1 python -m ntx.ops.bracket_probe
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, OrderType, TriggerType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy


class ProbeConfig(StrategyConfig, frozen=True):
    instrument_id: str
    side: str = "LONG"  # SHORT: a rising market triggers the stop


class BracketProbe(Strategy):
    def on_start(self) -> None:
        self.iid = InstrumentId.from_str(self.config.instrument_id)
        self.inst = self.cache.instrument(self.iid)
        self.subscribe_quote_ticks(self.iid)
        self.phase = 0
        self.tags: dict[str, str] = {}
        self.clock.set_timer("probe", timedelta(seconds=8), callback=self._tick)

    def say(self, msg: str) -> None:
        print(f"PROBE {self.clock.utc_now():%H:%M:%S.%f} {msg}", flush=True)

    def _bracket(self, sl_frac: float, label: str) -> None:
        q = self.cache.quote_tick(self.iid)
        short = self.config.side.upper() == "SHORT"
        px = float(q.ask_price if short else q.bid_price)
        sl = px * (1 + sl_frac) if short else px * (1 - sl_frac)
        b = self.order_factory.bracket(
            instrument_id=self.iid,
            order_side=OrderSide.SELL if short else OrderSide.BUY,
            quantity=self.inst.make_qty(Decimal("0.01")),
            entry_order_type=OrderType.MARKET,
            sl_trigger_price=self.inst.make_price(sl),
            sl_trigger_type=TriggerType.LAST_PRICE,
            tp_order_type=OrderType.MARKET_IF_TOUCHED,
            tp_trigger_price=self.inst.make_price(px * (0.5 if short else 1.5)),
            tp_trigger_type=TriggerType.LAST_PRICE,
        )
        for o in b.orders:
            self.tags[str(o.client_order_id)] = f"{label}:{o.order_type.name}"
        self.sl = next(o for o in b.orders if o.order_type == OrderType.STOP_MARKET)
        self.say(f"submit {label} px={px} sl={self.sl.trigger_price}")
        self.submit_order_list(b)

    def _tick(self, _e=None) -> None:
        if self.cache.quote_tick(self.iid) is None:
            return
        if self.phase == 0:
            self.phase = 1
            self._bracket(0.00001, "P1")
        elif self.phase == 1 and not self.cache.positions_open(instrument_id=self.iid):
            self.phase = 2
            self._bracket(0.10, "P2")
        elif self.phase == 2 and self.cache.positions_open(instrument_id=self.iid):
            self.phase = 3
            cached = self.cache.order(self.sl.client_order_id)
            self.say(f"P2 cancel child sl status={cached.status.name if cached else None}")
            if cached is not None:
                self.cancel_order(cached)
        elif self.phase == 3:
            self.phase = 4
            self.say("DONE")

    def _tag(self, e) -> str:
        return self.tags.get(str(e.client_order_id), "?")

    def on_order_accepted(self, e) -> None:
        self.say(f"ACCEPTED {self._tag(e)}")

    def on_order_filled(self, e) -> None:
        self.say(f"FILLED {self._tag(e)} qty={e.last_qty} px={e.last_px}")

    def on_order_triggered(self, e) -> None:
        self.say(f"TRIGGERED {self._tag(e)}")

    def on_order_canceled(self, e) -> None:
        self.say(f"CANCELED {self._tag(e)}")

    def on_order_rejected(self, e) -> None:
        self.say(f"REJECTED {self._tag(e)}: {e.reason}")

    def on_order_denied(self, e) -> None:
        self.say(f"DENIED {self._tag(e)}: {e.reason}")

    def on_order_cancel_rejected(self, e) -> None:
        self.say(f"CANCEL_REJECTED {self._tag(e)}: {e.reason}")

    def on_order_updated(self, e) -> None:
        self.say(f"UPDATED {self._tag(e)}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="LONG", choices=["LONG", "SHORT"])
    args = ap.parse_args(argv)
    from nautilus_trader.adapters.okx.factories import (
        OKXLiveDataClientFactory,
        OKXLiveExecClientFactory,
    )
    from nautilus_trader.live.node import TradingNode

    from ntx.config.geo import check_egress
    from ntx.config.settings import load
    from ntx.live.node import VENUE_NAME, node_config, plan, resolve_instruments
    from ntx.ops.flatten import main as flatten

    settings = load()
    if not settings.is_demo:
        print("REFUSING: demo-only")
        return 2
    if not check_egress().allowed:
        print("REFUSING: egress not allowed")
        return 2
    p = plan(settings, strategy_id="S900", instrument_ids=resolve_instruments("SOL"))
    node = TradingNode(config=node_config(p, settings))
    node.trader.add_strategy(
        BracketProbe(
            ProbeConfig(
                instrument_id=p.instrument_ids[0],
                side=args.side,
                use_hyphens_in_client_order_ids=False,
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
        flatten(["--demo", "--only", "SOL"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
