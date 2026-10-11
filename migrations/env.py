import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from bugalter.config import get_config
from bugalter.db import models  # noqa: F401  регистрирует таблицы
from bugalter.db.base import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=get_config().database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_config().database_url)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
