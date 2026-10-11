"""Отложенные ответы: пользователь узнаёт о результате только после фиксации транзакции.

Если транзакция откатилась, сообщения «Подтверждено» не уйдут — вместо них придёт ошибка.
"""

from collections.abc import Awaitable, Callable

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)

log = structlog.get_logger(__name__)

Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ForceReply | None
OnSent = Callable[[Message], Awaitable] | None


class Notifier:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._actions: list[Callable[[], Awaitable]] = []
        self._toast_sent = False

    def answer(self, message: Message, text: str, markup: Markup = None, on_sent: OnSent = None) -> None:
        self._actions.append(
            self._hooked(lambda: message.answer(text, reply_markup=markup, disable_web_page_preview=True), on_sent)
        )

    def send(self, chat_id: int, text: str, markup: Markup = None, on_sent: OnSent = None) -> None:
        self._actions.append(
            self._hooked(
                lambda: self.bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True),
                on_sent,
            )
        )

    @staticmethod
    def _hooked(action: Callable[[], Awaitable], on_sent: OnSent) -> Callable[[], Awaitable]:
        if on_sent is None:
            return action

        async def run():
            sent = await action()
            if sent is not None:
                try:
                    await on_sent(sent)
                except Exception as exc:  # noqa: BLE001 — сообщение уже ушло, это не повод падать
                    log.warning("on_sent_failed", error=str(exc))
            return sent

        return run

    def edit(self, message: Message | None, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        if message is None:
            return
        self._actions.append(lambda: message.edit_text(text, reply_markup=markup, disable_web_page_preview=True))

    def edit_in_chat(self, chat_id: int | None, message_id: int | None, text: str, markup=None) -> None:
        if not chat_id or not message_id:
            return
        self._actions.append(
            lambda: self.bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=markup, disable_web_page_preview=True
            )
        )

    def toast(self, callback: CallbackQuery, text: str = "", alert: bool = False) -> None:
        self._toast_sent = True
        self._actions.append(lambda: callback.answer(text, show_alert=alert))

    def document(self, chat_id: int, data: bytes, filename: str, caption: str | None = None) -> None:
        self._actions.append(
            lambda: self.bot.send_document(chat_id, BufferedInputFile(data, filename=filename), caption=caption)
        )

    def call(self, action: Callable[[], Awaitable]) -> None:
        self._actions.append(action)

    @property
    def toast_sent(self) -> bool:
        return self._toast_sent

    def discard(self) -> None:
        self._actions.clear()

    async def flush(self) -> list:
        results = []
        actions, self._actions = self._actions, []
        for action in actions:
            try:
                results.append(await action())
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc):
                    log.warning("telegram_bad_request", error=str(exc))
                results.append(None)
            except TelegramAPIError as exc:  # пользователь заблокировал бота и т.п.
                log.warning("telegram_send_failed", error=str(exc))
                results.append(None)
        return results
