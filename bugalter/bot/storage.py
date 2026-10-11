"""Хранилище состояний aiogram в PostgreSQL: незаконченный ввод переживает перезапуск бота."""

import datetime as dt
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StateType, StorageKey
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import FsmState


class PostgresStorage(BaseStorage):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker
        self.key_builder = DefaultKeyBuilder(with_bot_id=True, with_destiny=True)

    def _key(self, key: StorageKey) -> str:
        return self.key_builder.build(key)

    async def _upsert(self, key: str, **values) -> None:
        async with self.sessionmaker() as session, session.begin():
            stmt = insert(FsmState).values(key=key, **values)
            stmt = stmt.on_conflict_do_update(index_elements=[FsmState.key], set_={**values, "updated_at": func.now()})
            await session.execute(stmt)
            # Пустые записи (состояние сброшено, данных нет) не храним.
            await session.execute(
                delete(FsmState).where(FsmState.key == key, FsmState.state.is_(None), FsmState.data == {})
            )

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self._upsert(self._key(key), state=value)

    async def get_state(self, key: StorageKey) -> str | None:
        async with self.sessionmaker() as session:
            return (
                await session.execute(select(FsmState.state).where(FsmState.key == self._key(key)))
            ).scalar_one_or_none()

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        await self._upsert(self._key(key), data=dict(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self.sessionmaker() as session:
            data = (
                await session.execute(select(FsmState.data).where(FsmState.key == self._key(key)))
            ).scalar_one_or_none()
        return dict(data or {})

    async def close(self) -> None:
        pass

    async def cleanup(self, older_than: dt.timedelta = dt.timedelta(days=7)) -> int:
        """Удаляет брошенные незаконченные диалоги."""
        async with self.sessionmaker() as session, session.begin():
            result = await session.execute(delete(FsmState).where(FsmState.updated_at < func.now() - older_than))
            return result.rowcount or 0
