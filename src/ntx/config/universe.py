"""Trading universe and the Binance <-> OKX symbol map.

Why a map exists: price series come from `data.binance.vision` (free, long
history, reachable from Kazakhstan), while instrument specs, fees and precision
come from OKX, which is where we actually trade (ADR-0005). Cross-venue basis on
these majors was measured under 1bp, which is far below the fee and slippage
terms already modelled.

Universe choice: top-10 perps by liquidity, all listed well before the backtest
window and all still listed -- picking today's winners retroactively would make
the backtest a lie (survivorship bias, docs/09 §2).
"""

from __future__ import annotations

from dataclasses import dataclass

OKX_VENUE_SUFFIX = ".OKX"


@dataclass(frozen=True)
class Asset:
    base: str
    binance_symbol: str  # USDT-M perp on Binance, for the data dumps
    okx_inst_id: str  # linear swap on OKX, for trading

    @property
    def nautilus_id(self) -> str:
        """Fully-qualified nautilus InstrumentId, e.g. 'BTC-USDT-SWAP.OKX'.

        The venue suffix is what the catalog and the engine key on, while the
        bare `okx_inst_id` is what OKX's REST API uses. Keeping the conversion
        here avoids concatenating it at every call site and getting it wrong in
        one of them.
        """
        return f"{self.okx_inst_id}{OKX_VENUE_SUFFIX}"


UNIVERSE: tuple[Asset, ...] = (
    Asset("BTC", "BTCUSDT", "BTC-USDT-SWAP"),
    Asset("ETH", "ETHUSDT", "ETH-USDT-SWAP"),
    Asset("SOL", "SOLUSDT", "SOL-USDT-SWAP"),
    Asset("BNB", "BNBUSDT", "BNB-USDT-SWAP"),
    Asset("XRP", "XRPUSDT", "XRP-USDT-SWAP"),
    Asset("DOGE", "DOGEUSDT", "DOGE-USDT-SWAP"),
    Asset("ADA", "ADAUSDT", "ADA-USDT-SWAP"),
    Asset("LINK", "LINKUSDT", "LINK-USDT-SWAP"),
    Asset("AVAX", "AVAXUSDT", "AVAX-USDT-SWAP"),
    Asset("TRX", "TRXUSDT", "TRX-USDT-SWAP"),
)

BY_BINANCE = {a.binance_symbol: a for a in UNIVERSE}
BY_OKX = {a.okx_inst_id: a for a in UNIVERSE}
BY_BASE = {a.base: a for a in UNIVERSE}

# Earliest month with USDT-M perp data on Binance. Per-asset listing dates are
# discovered at download time (a 404 before listing is expected, not an error) --
# we must never trade an asset before it existed.
BACKTEST_START = "2020-01"

# Aggregations built into the catalog from the 1m source.
TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h")
PRIMARY_TIMEFRAME = "1h"


def binance_symbols() -> list[str]:
    return [a.binance_symbol for a in UNIVERSE]
