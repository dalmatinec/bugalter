from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Секреты и параметры запуска. Всё, что меняется во время работы, хранится в таблице settings."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(default="", alias="BOT_TOKEN")
    database_url: str = Field(
        default="postgresql+asyncpg://bugalter:bugalter@localhost:5432/bugalter", alias="DATABASE_URL"
    )
    # Telegram ID владельцев: при старте им выдаётся роль owner (идемпотентно).
    owner_telegram_ids: list[int] = Field(default_factory=list, alias="OWNER_TELEGRAM_IDS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_json: bool = Field(default=True, alias="LOG_JSON")

    @field_validator("owner_telegram_ids", mode="before")
    @classmethod
    def _split_ids(cls, value):
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        if isinstance(value, int):
            return [value]
        return value


@lru_cache
def get_config() -> Config:
    return Config()
