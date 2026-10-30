from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWLISTED_CHAIN_IDS = {80002, 31337}
TOKEN_NAME = "USD Coin (Test)"
TOKEN_VERSION = "1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    key_encryption_key: str
    database_url: str
    rpc_url: str
    chain_id: int
    relayer_private_key: str
    router_address: str
    usdc_address: str
    relayer_pol_threshold: int = 10**16
    max_fee_per_gas: int = 500 * 10**9
    max_priority_fee_per_gas: int = 50 * 10**9
    daily_send_cap: int = 500 * 10**6
    starter_mint: int = 100 * 10**6
    faucet_mint: int = 100 * 10**6
    faucet_daily_cap: int = 100 * 10**6
    hourly_transfer_limit: int = 10
    parse_limit_per_minute: int = 20
    confirm_ttl_seconds: int = 600
    permit_ttl_seconds: int = 3600
    rebroadcast_seconds: int = 15
    bump_after_seconds: int = 120
    sim_attempt_limit: int = 5
    admin_telegram_id: int | None = None

    @field_validator("admin_telegram_id", mode="before")
    @classmethod
    def blank_admin(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("chain_id")
    @classmethod
    def allowlisted(cls, value: int) -> int:
        if value not in ALLOWLISTED_CHAIN_IDS:
            raise ValueError("CHAIN_ID must be 80002 (Amoy) or 31337 (Anvil).")
        return value

    @field_validator("key_encryption_key")
    @classmethod
    def encryption_key(cls, value: str) -> str:
        raw = bytes.fromhex(value.removeprefix("0x"))
        if len(raw) != 32:
            raise ValueError("KEY_ENCRYPTION_KEY must be 32 bytes of hex.")
        return value.removeprefix("0x")


def get_settings() -> Settings:
    return Settings()
