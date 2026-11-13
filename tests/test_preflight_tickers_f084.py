"""F084: one empty ticker took all three bots down for 38 hours.

2026-10-24: OKX listed TEST002-USDT-SWAP / TEST003-USDT-SWAP with `last: ""`.
`run_preflight` did `float(t["last"])` over the WHOLE swap universe, so every
start raised ValueError; an OS update had just restarted the bots, systemd
retried 10 times and gave up -- live down with an AVAX short open (R73).

Written before the fix (R41).
"""

from __future__ import annotations

from ntx.live.node import parse_tickers

OURS = {"BTC-USDT-SWAP.OKX", "AVAX-USDT-SWAP.OKX"}


def test_an_unrelated_contract_with_no_price_does_not_stop_the_start():
    data = [
        {"instId": "BTC-USDT-SWAP", "last": "80517.7"},
        {"instId": "AVAX-USDT-SWAP", "last": "8.959"},
        {"instId": "TEST002-USDT-SWAP", "last": ""},
        {"instId": "TEST003-USDT-SWAP", "last": ""},
    ]
    prices, bad = parse_tickers(data, OURS)
    assert prices == {"BTC-USDT-SWAP.OKX": 80517.7, "AVAX-USDT-SWAP.OKX": 8.959}
    assert bad == []  # not ours: not even worth a warning


def test_our_own_instrument_with_no_price_is_reported_not_fatal():
    data = [
        {"instId": "BTC-USDT-SWAP", "last": "80517.7"},
        {"instId": "AVAX-USDT-SWAP", "last": ""},
    ]
    prices, bad = parse_tickers(data, OURS)
    assert prices == {"BTC-USDT-SWAP.OKX": 80517.7}
    assert bad == ["AVAX-USDT-SWAP.OKX"]


def test_garbage_and_non_positive_prices_are_not_prices():
    data = [{"instId": "BTC-USDT-SWAP", "last": "0"}, {"instId": "AVAX-USDT-SWAP"}]
    prices, bad = parse_tickers(data, OURS)
    assert prices == {} and sorted(bad) == sorted(OURS)
