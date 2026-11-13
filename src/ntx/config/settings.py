"""Runtime settings, loaded from .env.

No secret has a default. Paths are absolute and validated at startup, never at
first use -- systemd's working directory is not the repo (R21).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

# The live key in OKX_API_* belongs to S003 and only to S003. Every other live
# strategy runs on its own OKX sub-account with its own key (ADR-0014 + its
# 2026-10-07 amendment): two nodes on one account adopt and close each other's
# positions (R51, F046, F064).
LIVE_DEFAULT_KEY_OWNER = "S003"
DEMO_KEYSETS = ("demo1", "demo2")
# Loss limits that leave the guard able to fire (E3-04). Ranges, not values:
# the values are the owner's (R14) and live in docs/08.
DAILY_LOSS_RANGE = (0.0, 20.0)
DRAWDOWN_RANGE = (0.0, 50.0)


def _lookup(name: str) -> str:
    """A per-strategy key variable: the process environment, then `.env`.

    These names are not fields (one set per strategy), so pydantic does not
    read them; this reads them the same two ways, environment first.
    """
    if os.environ.get(name):
        return os.environ[name]
    if ENV_FILE.is_file():
        from dotenv import dotenv_values

        return dotenv_values(ENV_FILE).get(name) or ""
    return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        # A blank line in .env means "not set", not "the empty string". docs/21
        # prescribes a blank NTX_EQUITY_OVERRIDE_USD; without this it raised a
        # ValidationError inside the live node, doctor, the alerter and the kill
        # switch (D-01/R-03, review 2026-09-18).
        env_ignore_empty=True,
    )

    # --- identity / routing ---
    venue: str = Field(default="okx_live", alias="NTX_VENUE")
    strategy: str = Field(default="S003", alias="NTX_STRATEGY")
    trader_id: str = Field(default="NTX-001", alias="NTX_TRADER_ID")
    log_level: str = Field(default="INFO", alias="NTX_LOG_LEVEL")
    data_dir: Path = Field(default=REPO_ROOT / "data", alias="NTX_DATA_DIR")

    # --- OKX ---
    okx_api_key: str = Field(default="", alias="OKX_API_KEY")
    okx_api_secret: str = Field(default="", alias="OKX_API_SECRET")
    okx_api_passphrase: str = Field(default="", alias="OKX_API_PASSPHRASE")
    # A SECOND demo sub-account, for a second strategy. Two nodes on one OKX
    # account fight over the same positions (F046/R51): each adopts the other's
    # position as its own and the orphan repair closes it. Measured, not feared --
    # drills sharing the soak's account put trades into the S003 journal that the
    # strategy never chose (F064).
    keyset: str = Field(default="", alias="NTX_KEYSET")
    okx_demo2_api_key: str = Field(default="", alias="OKX_DEMO2_API_KEY")
    okx_demo2_api_secret: str = Field(default="", alias="OKX_DEMO2_API_SECRET")
    okx_demo2_api_passphrase: str = Field(default="", alias="OKX_DEMO2_API_PASSPHRASE")
    okx_demo_api_key: str = Field(default="", alias="OKX_DEMO_API_KEY")
    okx_demo_api_secret: str = Field(default="", alias="OKX_DEMO_API_SECRET")
    okx_demo_api_passphrase: str = Field(default="", alias="OKX_DEMO_API_PASSPHRASE")
    okx_region: str = Field(default="GLOBAL", alias="OKX_REGION")
    okx_base_url_http: str = Field(default="https://www.okx.com", alias="OKX_BASE_URL_HTTP")
    okx_public_base_url: str = Field(default="https://my.okx.com", alias="OKX_PUBLIC_BASE_URL")
    okx_proxy_url: str = Field(default="", alias="OKX_PROXY_URL")

    # --- risk caps (hard, enforced pre-trade) ---
    equity_ccy: str = Field(default="USDT", alias="NTX_EQUITY_CCY")
    equity_override_usd: float | None = Field(default=None, alias="NTX_EQUITY_OVERRIDE_USD")
    risk_equity_pct: float = Field(default=0.5, alias="NTX_RISK_EQUITY_PCT")
    max_leverage: float = Field(default=3.0, alias="NTX_MAX_LEVERAGE")
    max_concurrent_positions: int = Field(default=3, alias="NTX_MAX_CONCURRENT_POSITIONS")
    daily_loss_limit_equity_pct: float = Field(default=5.0, alias="NTX_DAILY_LOSS_LIMIT_EQUITY_PCT")
    max_drawdown_equity_pct: float = Field(default=20.0, alias="NTX_MAX_DRAWDOWN_EQUITY_PCT")

    # --- alerting (docs/23 §4): push + dead-man switch, both optional ---
    alert_ntfy_url: str = Field(default="", alias="NTX_ALERT_NTFY_URL")
    healthcheck_url: str = Field(default="", alias="NTX_HEALTHCHECK_URL")
    # Ping the phone when a trade opens or closes, not only when something is
    # wrong. Set to 0 to keep alerts to incidents only.
    alert_fills: bool = Field(default=True, alias="NTX_ALERT_FILLS")
    # Let nautilus reconstruct an opening order it cannot find, so a restart can
    # ADOPT a position instead of abandoning it unmanaged (F071). OFF by default:
    # with the old 60-minute fill search this invented openers on nearly every
    # restart and produced the phantom SHORTs behind F062/F069. F071 widened that
    # search to 7 days, which is why it is worth revisiting -- on demo first (R44).
    generate_missing_orders: bool = Field(default=False, alias="NTX_GENERATE_MISSING_ORDERS")
    # Bots that MUST be running, e.g. "S003-live"; they alarm when stale at any age.
    alert_bots: str = Field(default="", alias="NTX_ALERT_BOTS")
    # Attach the venue stop to the entry order (G7). Changes execution semantics
    # (intrabar stops) -- re-run the S003 gate before enabling for real money.
    attach_stop_at_entry: bool = Field(default=False, alias="NTX_ATTACH_STOP_AT_ENTRY")

    # --- telegram (deferred: enabled later, must not block trading) ---
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    @field_validator("venue", mode="before")
    @classmethod
    def _check_venue(cls, v: object) -> str:
        """`<exchange>_live` or `<exchange>_demo`, exactly.

        `is_demo` used to be `endswith("_demo")`, so `okx-demo`, `okxdemo` and
        `okx_paper` all meant the LIVE account with the LIVE keys (L-11).
        """
        s = str(v or "").strip()
        if not re.fullmatch(r"[a-z0-9]+_(live|demo)", s):
            raise ValueError(f"NTX_VENUE={s!r} must be '<exchange>_live' or '<exchange>_demo'")
        return s

    @field_validator("data_dir", mode="before")
    @classmethod
    def _resolve_data_dir(cls, v: object) -> Path:
        if v in (None, ""):
            return REPO_ROOT / "data"
        p = Path(str(v)).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()

    @model_validator(mode="after")
    def _check_ranges(self) -> Settings:
        if not 0 < self.risk_equity_pct <= 10:
            raise ValueError("NTX_RISK_EQUITY_PCT must be in (0, 10]")
        if not 1 <= self.max_leverage <= 10:
            raise ValueError("NTX_MAX_LEVERAGE must be in [1, 10]")
        if self.max_concurrent_positions < 1:
            raise ValueError("NTX_MAX_CONCURRENT_POSITIONS must be >= 1")
        # 0 or a negative value disables PAUSE/KILL; a huge one makes KILL
        # unreachable. Either removes a guard from the live account without a
        # word (E3-04).
        lo, hi = DAILY_LOSS_RANGE
        if not lo < self.daily_loss_limit_equity_pct <= hi:
            raise ValueError(f"NTX_DAILY_LOSS_LIMIT_EQUITY_PCT must be in ({lo:g}, {hi:g}]")
        lo, hi = DRAWDOWN_RANGE
        if not lo < self.max_drawdown_equity_pct <= hi:
            raise ValueError(f"NTX_MAX_DRAWDOWN_EQUITY_PCT must be in ({lo:g}, {hi:g}]")
        return self

    # --- derived ---
    @property
    def is_demo(self) -> bool:
        return self.venue.endswith("_demo")

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def creds(self, strategy: str | None = None) -> tuple[str, str, str]:
        """Return (key, secret, passphrase) for the selected environment.

        `NTX_KEYSET` picks the sub-account. Demo: `demo1` (default, OKX_DEMO_*)
        or `demo2` (OKX_DEMO2_*). Live: `S003` (default, OKX_API_*) or another
        strategy id, e.g. `S005` -> OKX_S005_API_KEY / _SECRET / _PASSPHRASE.

        `strategy` is the node's strategy id; with it, a live key set must
        belong to that strategy, and only S003 may use OKX_API_* (E3-03). Every
        wrong value raises rather than falling back: a silent fallback would
        put two nodes on one sub-account, which is the precise failure this
        exists to prevent (R51).
        """
        ks = self.keyset.strip()
        if self.is_demo:
            if ks in ("", "demo1"):
                return (
                    self.okx_demo_api_key,
                    self.okx_demo_api_secret,
                    self.okx_demo_api_passphrase,
                )
            if ks != "demo2":
                raise ValueError(f"unknown NTX_KEYSET={ks!r}; known on demo: {DEMO_KEYSETS}")
            creds = (
                self.okx_demo2_api_key,
                self.okx_demo2_api_secret,
                self.okx_demo2_api_passphrase,
            )
            if not all(creds):
                raise ValueError(
                    "NTX_KEYSET=demo2 but OKX_DEMO2_API_KEY / _SECRET / _PASSPHRASE are not all set"
                )
            return creds
        if ks.lower().startswith("demo"):
            raise ValueError(
                f"NTX_KEYSET={ks!r} is a demo key set on a non-demo venue ({self.venue})"
            )
        owner = ks.upper() or LIVE_DEFAULT_KEY_OWNER
        if not re.fullmatch(r"S\d{3}", owner):
            raise ValueError(f"unknown live NTX_KEYSET={ks!r}; use the strategy id, e.g. 'S005'")
        sid = strategy.upper() if strategy else None
        if sid is not None and sid != owner:
            if owner == LIVE_DEFAULT_KEY_OWNER:
                raise ValueError(
                    f"{sid} would run on {LIVE_DEFAULT_KEY_OWNER}'s live key (OKX_API_*): "
                    f"one live sub-account and key per strategy (ADR-0014, R51). Set "
                    f"NTX_KEYSET={sid} and OKX_{sid}_API_KEY / _SECRET / _PASSPHRASE"
                )
            raise ValueError(f"NTX_KEYSET={owner} is {owner}'s live key; {sid} may not use it")
        live = (self.okx_api_key, self.okx_api_secret, self.okx_api_passphrase)
        if owner == LIVE_DEFAULT_KEY_OWNER:
            return live
        names = [f"OKX_{owner}_API_{p}" for p in ("KEY", "SECRET", "PASSPHRASE")]
        creds = tuple(_lookup(n) for n in names)
        if not all(creds):
            raise ValueError(f"NTX_KEYSET={owner} but {' / '.join(names)} are not all set")
        if creds[0] == live[0]:
            raise ValueError(
                f"OKX_{owner}_API_KEY is the same key as {LIVE_DEFAULT_KEY_OWNER}'s: that is "
                f"the same sub-account, not a separate one (R51)"
            )
        return creds  # type: ignore[return-value]

    def required_paths(self) -> list[Path]:
        return [
            self.data_dir,
            self.data_dir / "raw",
            self.data_dir / "catalog",
            REPO_ROOT / "logs",
            REPO_ROOT / "reports",
        ]


def load() -> Settings:
    return Settings()  # type: ignore[call-arg]
