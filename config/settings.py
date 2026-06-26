"""Single source of truth for all bot configuration.

Secrets come from the environment (.env); tunables have safe, validated defaults.
The model is frozen, so nothing can relax a limit at runtime, and a mode/testnet
interlock refuses to boot in dangerous combinations (e.g. live mode with testnet keys).
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"  # Binance Spot Testnet
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        frozen=True,
        extra="ignore",
        populate_by_name=True,  # allow Settings(mode=...) as well as the MODE alias
    )

    # --- secrets (from .env only; never defaulted to a real value) ---
    binance_api_key: str = Field(default="", alias="BINANCE_API_KEY")
    binance_api_secret: str = Field(default="", alias="BINANCE_API_SECRET")
    binance_testnet: bool = Field(default=True, alias="BINANCE_TESTNET")

    # --- runtime ---
    mode: Mode = Field(default=Mode.BACKTEST, alias="MODE")
    exchange: str = Field(default="binance", alias="EXCHANGE")
    # NoDecode: don't JSON-parse the env value; our validator splits a plain/comma string
    pairs: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["BTC/USDT"], alias="PAIRS")
    timeframe: str = Field(default="1d", alias="TIMEFRAME")
    strategy: str = Field(default="donchian", alias="STRATEGY")

    # --- indicator params (trend following, per recommended-model.md) ---
    ema_trend: int = Field(default=200, gt=1, alias="EMA_TREND")
    atr_period: int = Field(default=14, gt=1, alias="ATR_PERIOD")
    atr_stop_mult: float = Field(default=2.0, gt=0, alias="ATR_STOP_MULT")
    donchian_entry: int = Field(default=20, gt=1, alias="DONCHIAN_ENTRY")
    donchian_exit: int = Field(default=10, gt=1, alias="DONCHIAN_EXIT")

    # --- mean-reversion / regime-switch params ---
    bb_period: int = Field(default=20, gt=1, alias="BB_PERIOD")
    bb_std: float = Field(default=2.0, gt=0, alias="BB_STD")
    chop_period: int = Field(default=14, gt=1, alias="CHOP_PERIOD")
    chop_threshold: float = Field(default=50.0, gt=0, lt=100, alias="CHOP_THRESHOLD")

    # --- confluence strategy params (alternative, spec §4.4) ---
    rsi_period: int = Field(default=14, gt=1, alias="RSI_PERIOD")
    rsi_buy: float = Field(default=35, ge=0, le=100, alias="RSI_BUY")
    rsi_exit: float = Field(default=55, ge=0, le=100, alias="RSI_EXIT")
    reward_risk: float = Field(default=1.5, gt=0, alias="REWARD_RISK")

    # --- risk params (hard limits) ---
    risk_per_trade_pct: float = Field(default=1.0, gt=0, le=5, alias="RISK_PER_TRADE_PCT")
    max_open_positions: int = Field(default=2, ge=1, le=1000, alias="MAX_OPEN_POSITIONS")
    max_daily_loss_pct: float = Field(default=5.0, gt=0, le=50, alias="MAX_DAILY_LOSS_PCT")
    max_consecutive_errors: int = Field(default=5, ge=1, alias="MAX_CONSECUTIVE_ERRORS")
    live_capital_cap: float = Field(default=100.0, gt=0, alias="LIVE_CAPITAL_CAP")
    max_drawdown_pct: float = Field(default=25.0, gt=0, le=100, alias="MAX_DRAWDOWN_PCT")
    max_total_exposure: float = Field(default=3.0, gt=0, alias="MAX_TOTAL_EXPOSURE")  # sum(notional)/equity cap

    # --- notifications (optional; Telegram) ---
    telegram_token: str = Field(default="", alias="TELEGRAM_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    funding_rate_estimate: float = Field(default=0.0001, alias="FUNDING_RATE_ESTIMATE")

    # --- backtest realism ---
    initial_capital: float = Field(default=400.0, gt=0, alias="INITIAL_CAPITAL")
    taker_fee_pct: float = Field(default=0.1, ge=0, alias="TAKER_FEE_PCT")
    slippage_bps: float = Field(default=5.0, ge=0, alias="SLIPPAGE_BPS")

    # --- paths ---
    state_path: str = Field(default="data_store/state.json", alias="STATE_PATH")
    log_dir: str = Field(default="logs", alias="LOG_DIR")

    @field_validator("pairs", mode="before")
    @classmethod
    def _split_pairs(cls, v):
        if isinstance(v, str):
            return [p.strip().upper() for p in v.split(",") if p.strip()]
        return v

    @model_validator(mode="after")
    def _mode_key_interlock(self):
        # network modes need keys
        if self.mode in (Mode.PAPER, Mode.LIVE) and not (
            self.binance_api_key and self.binance_api_secret
        ):
            raise ValueError(
                f"MODE={self.mode.value} requires BINANCE_API_KEY/SECRET in .env"
            )
        # never fire real orders thinking you are on testnet (and vice versa)
        if self.mode == Mode.LIVE and self.binance_testnet:
            raise ValueError(
                "MODE=live but BINANCE_TESTNET=true. Refusing to start (ambiguous keys)."
            )
        if self.mode == Mode.PAPER and not self.binance_testnet:
            raise ValueError("MODE=paper requires BINANCE_TESTNET=true.")
        return self


def load_settings(**overrides) -> Settings:
    """Build a Settings instance. `overrides` win over .env/defaults (used by the CLI)."""
    return Settings(**{k: v for k, v in overrides.items() if v is not None})
