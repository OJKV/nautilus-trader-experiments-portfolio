"""DEMO-ONLY probe: which protective-stop form does OKX accept? (F036)

Opens a 0.01-contract SOL position three times and tries one stop form each:
  V1 standalone stop-market, reduce_only=False
  V2 standalone stop-market, reduce_only=True + close_fraction="1"
  V3 attached SL on the entry order (bracket; far MIT take-profit is required
     by the nautilus factory)
Each phase ends by cancelling all orders and closing the position. Refuses to
run outside the demo environment.

    NTX_VENUE=okx_demo NTX_EQUITY_OVERRIDE_USD=1 python -m ntx.ops.stop_probe
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce, TriggerType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy


class ProbeConfig(StrategyConfig, frozen=True):
    instrument_id: str


class StopProbe(Strategy):
    def on_start(self) -> None:
        self.iid = InstrumentId.from_str(self.config.instrument_id)
        self.inst = self.cache.instrument(self.iid)
        self.subscribe_quote_ticks(self.iid)
        self.phase = 0
        self.stops: dict[str, str] = {}
        self.clock.set_timer("probe", timedelta(seconds=12), callback=self._next)

    def _px(self) -> float:
        q = self.cache.quote_tick(self.iid)
        return float(q.bid_price) if q else 0.0

    def _next(self, _e=None) -> None:
        self.cancel_all_orders(self.iid)
        self.close_all_positions(self.iid)
        self.phase += 1
        px = self._px()
        if px <= 0:
            print("PROBE no price yet", flush=True)
            self.phase -= 1
            return
        qty = self.inst.make_qty(Decimal("0.01"))
        sl = self.inst.make_price(px * 0.9)
        if self.phase in (1, 2):
            self.submit_order(
                self.order_factory.market(
                    self.iid, OrderSide.BUY, qty, time_in_force=TimeInForce.GTC
                )
            )
        elif self.phase == 3:
            bracket = self.order_factory.bracket(
                instrument_id=self.iid,
                order_side=OrderSide.BUY,
                quantity=qty,
                entry_order_type=OrderType.MARKET,
                sl_trigger_price=sl,
                sl_trigger_type=TriggerType.LAST_PRICE,
                tp_order_type=OrderType.MARKET_IF_TOUCHED,
                tp_trigger_price=self.inst.make_price(px * 1.5),
                tp_trigger_type=TriggerType.LAST_PRICE,
            )
            for o in bracket.orders:
                self.stops[str(o.client_order_id)] = f"V3 {o.order_type.name}"
            self.submit_order_list(bracket)
        else:
            print("PROBE DONE", flush=True)
            self.clock.cancel_timer("probe")

    def on_order_filled(self, event) -> None:
        if self.phase not in (1, 2) or event.order_side != OrderSide.BUY:
            return
        if any(v.startswith(f"V{self.phase}") for v in self.stops.values()):
            return
        qty = self.inst.make_qty(Decimal("0.01"))
        sl = self.inst.make_price(self._px() * 0.9)
        o = self.order_factory.stop_market(
            self.iid,
            OrderSide.SELL,
            qty,
            trigger_price=sl,
            trigger_type=TriggerType.LAST_PRICE,
            reduce_only=(self.phase == 2),
            time_in_force=TimeInForce.GTC,
        )
        self.stops[str(o.client_order_id)] = f"V{self.phase}"
        params = {"close_fraction": "1"} if self.phase == 2 else None
        self.submit_order(o, params=params)

    def on_order_accepted(self, event) -> None:
        tag = self.stops.get(str(event.client_order_id))
        if tag:
            print(f"PROBE ACCEPTED {tag}", flush=True)

    def on_order_rejected(self, event) -> None:
        tag = self.stops.get(str(event.client_order_id), "entry")
        print(f"PROBE REJECTED {tag}: {event.reason}", flush=True)

    def on_order_denied(self, event) -> None:
        tag = self.stops.get(str(event.client_order_id), "entry")
        print(f"PROBE DENIED {tag}: {event.reason}", flush=True)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.iid)
        self.close_all_positions(self.iid)


def main() -> int:
    from nautilus_trader.adapters.okx.factories import (
        OKXLiveDataClientFactory,
        OKXLiveExecClientFactory,
    )
    from nautilus_trader.live.node import TradingNode

    from ntx.config.geo import check_egress
    from ntx.config.settings import load
    from ntx.live.node import VENUE_NAME, node_config, plan, resolve_instruments

    settings = load()
    if not settings.is_demo:
        print("REFUSING: stop_probe is demo-only")
        return 2
    if not check_egress().allowed:
        print("REFUSING: egress not allowed")
        return 2
    p = plan(settings, strategy_id="S900", instrument_ids=resolve_instruments("SOL"))
    node = TradingNode(config=node_config(p, settings))
    node.trader.add_strategy(
        StopProbe(
            ProbeConfig(instrument_id=p.instrument_ids[0], use_hyphens_in_client_order_ids=False)
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
