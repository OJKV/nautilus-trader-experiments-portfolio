# NautilusTrader experiments

Research and engineering experiments using NautilusTrader **1.230.0**. The repository includes the S003 Donchian breakout strategy, historical backtesting, data integrity checks, risk controls, and a read-only monitoring interface. Historical simulations are not evidence of future returns or live performance.

Read the [research reference](docs/RESEARCH_REFERENCE.md) for the historical results, methods and limitations.

## Local development

Use Python 3.12 and install the project and development dependencies in a virtual environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev,ui]"
.venv/bin/python -m pytest -m "not live and not vpn"
```

The historical market catalog and generated reports are not included. Backtest reproduction requires preparing the catalog and funding data separately; results depend on the data, revision, execution assumptions, and costs. Some tests require local recorded artifacts and may skip if those artifacts are absent.

The code includes live trading and operational components. Review their configuration and risks before use. No exchange credentials are needed for the offline tests.

## Optional account-sized research

The optional `--live-config` research run needs `docs/private/s003-live-config.json`. Create it locally with an `equity_usd` number and an `assets` list, for example `{ "equity_usd": 10000, "assets": ["BTC", "ETH"] }`. This example is synthetic; supply and verify your own current inputs before using that mode. The default historical gate does not need this file. The optional research run is a backtest and does not start a trading bot.
