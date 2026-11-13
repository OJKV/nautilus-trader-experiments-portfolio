# S003 historical research reference

S003 is a rules-based Donchian breakout experiment, not a machine-learning model. It uses a 55-bar entry channel and 20-bar exit channel on four-hour bars, with a two-ATR stop, a trailing stop and risk-based sizing. The reference run used ten crypto perpetual markets, a 0.5% risk setting and a maximum of three concurrent positions. The results below are historical simulations; they are not live returns or forecasts.

## Reference results

The reference gate was rerun on 13 November 2026. The in-sample window was 1 January 2020 to 31 December 2023. The later window was 1 January 2024 to 31 August 2026. The latter was already examined during earlier research; this rerun is not an independent fresh holdout.

| Window | Trades | Return before funding | Annualized Sharpe | Maximum drawdown | Profit factor after funding |
| --- | ---: | ---: | ---: | ---: | ---: |
| In-sample | 1,304 | 63% | 0.85 | 17.7% | 1.13 |
| Previously inspected later window | 812 | 80% | 1.62 | 16.5% | 1.30 |

The source revision at the rerun was `223c63c7705d408bce37dd47524821f00328b9f8` with uncommitted privacy changes. Its recorded source hash was `f66331727dbd1dc8` and catalog fingerprint `0f6eaa63eaedd30c`. The source revision identifies the original research checkout, not this publication copy.

## Data, timing and costs

The catalog used Binance USDT-margined perpetual one-minute bars, aggregated into four-hour bars, and historical eight-hour funding rates. Integrity checks cover timestamps, duplicates, gaps and OHLC validity. Asset histories start at their available listing dates. The bars are stamped at their close to prevent the engine receiving a close before it occurs. The simulation fills a market order at the signal bar close; this assumption can be optimistic.

The reference charges a 0.05% taker fee per side plus two basis points of slippage per side. Slippage is included in the effective trading fee. Funding is applied to closed positions afterward using historical rates and holding direction. It adjusts the reported profit factor but is not compounded through the equity curve, so the Sharpe and drawdown figures do not fully reflect funding.

A recorded 40-seed random-entry control is available in [the reference manifest](../tests/fixtures/random_control_manifest.json). It is an historical comparison, not a new validation. Market data and the full gate-run artifacts are not included; reproducing the run requires matching data, software and assumptions. Market impact, execution delays and changes in regime can make realized outcomes differ materially.
