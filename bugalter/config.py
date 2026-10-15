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
    # Telegram ID владельцев через запятую: при старте им выдаётся роль owner (идемпотентно).
    # Строка, а не list[int]: pydantic-settings читает списки из окружения как JSON и падает на «1,2».
    owner_ids_raw: str = Field(default="", alias="OWNER_TELEGRAM_IDS")
    # Необязательно: прокси до api.telegram.org, если сервер не может достучаться до Telegram напрямую.
    telegram_proxy: str | None = Field(default=None, alias="TELEGRAM_PROXY")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_json: bool = Field(default=True, alias="LOG_JSON")
    # Папка с резервными копиями (общая с контейнером backup): бот отдаёт копии и принимает файл для восстановления.
    backup_dir: str = Field(default="/backups", alias="BACKUP_DIR")

    @field_validator("owner_ids_raw", mode="before")
    @classmethod
    def _check_ids(cls, value) -> str:
        text = str(value or "").replace(";", ",")
        for part in text.split(","):
            if part.strip() and not part.strip().lstrip("-").isdigit():
                raise ValueError(f"OWNER_TELEGRAM_IDS: {part.strip()} не числовой Telegram ID")
        return text

    @property
    def owner_telegram_ids(self) -> list[int]:
        return [int(part) for part in self.owner_ids_raw.split(",") if part.strip()]


@lru_cache
def get_config() -> Config:
    return Config()
