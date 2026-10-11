"""Одна транзакция БД на одно действие пользователя.

Порядок: загрузить пользователя и права → выполнить обработчик → COMMIT → отправить ответы.
Ошибка бизнес-логики откатывает транзакцию и показывается пользователю текстом.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Config
from ..domain.errors import DomainError
from ..services import access, org
from ..services.settings import get_settings
from .notify import Notifier

log = structlog.get_logger(__name__)


def _db_error_text(exc: DBAPIError) -> str | None:
    text = str(exc.orig) if exc.orig is not None else str(exc)
    if "Расчётный период" in text and "закрыт" in text:
        return "Расчётный период закрылся во время операции. Повторите действие — запись попадёт в открытый период."
    if "ck_stock_balances_non_negative" in text:
        return "Недостаточно товара: остаток не может стать отрицательным."
    if "только добавлением" in text or "нельзя" in text:
        return "Операция запрещена правилами учёта: проведённые записи не изменяются и не удаляются."
    if "could not serialize" in text or "deadlock" in text:
        return "Параллельное изменение тех же данных. Повторите действие."
    return None


class ContextMiddleware(BaseMiddleware):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], config: Config):
        self.sessionmaker = sessionmaker
        self.config = config

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        chat = data.get("event_chat")
        if tg_user is None or tg_user.is_bot:
            return None
        notifier = Notifier(data["bot"])
        structlog.contextvars.bind_contextvars(tg_user=tg_user.id, chat=chat.id if chat else None)
        try:
            async with self.sessionmaker() as session:
                async with session.begin():
                    actor = await access.load_actor(session, tg_user.id, tg_user.username, tg_user.full_name)
                    settings = await get_settings(session)
                    chat_dept = chat_employee = None
                    if chat is not None and chat.type != ChatType.PRIVATE:
                        chat_dept, chat_employee = await org.chat_binding(session, chat.id)
                    data.update(
                        session=session,
                        actor=actor,
                        settings=settings,
                        chat_dept=chat_dept,
                        chat_employee=chat_employee,
                        notifier=notifier,
                        config=self.config,
                        sessionmaker=self.sessionmaker,
                        is_private=chat is not None and chat.type == ChatType.PRIVATE,
                    )
                    result = await handler(event, data)
            if isinstance(event, CallbackQuery) and not notifier.toast_sent:
                notifier.toast(event)
            await notifier.flush()
            return result
        except DomainError as exc:
            notifier.discard()
            await self._reply(event, f"⚠️ {exc}", notifier)
        except DBAPIError as exc:
            notifier.discard()
            message = _db_error_text(exc)
            if message is None:
                await self._internal_error(event, exc, notifier, data)
            else:
                log.info("db_rule_violation", error=str(exc.orig))
                await self._reply(event, f"⚠️ {message}", notifier)
        except Exception as exc:  # noqa: BLE001 — сообщаем пользователю и владельцам, затем логируем
            notifier.discard()
            await self._internal_error(event, exc, notifier, data)
        finally:
            structlog.contextvars.clear_contextvars()
        return None

    async def _reply(self, event: TelegramObject, text: str, notifier: Notifier) -> None:
        if isinstance(event, CallbackQuery):
            notifier.toast(event, text[:190], alert=True)
        elif isinstance(event, Message):
            notifier.answer(event, text)
        await notifier.flush()

    async def _internal_error(self, event, exc: Exception, notifier: Notifier, data: dict) -> None:
        error_id = uuid.uuid4().hex[:8]
        log.exception("unhandled_error", error_id=error_id, error=str(exc))
        await self._reply(event, f"❌ Внутренняя ошибка (код {error_id}). Данные не изменены.", notifier)
        for owner_id in self.config.owner_telegram_ids:
            notifier.send(owner_id, f"❌ Ошибка {error_id}: {type(exc).__name__}: {str(exc)[:500]}")
        await notifier.flush()
