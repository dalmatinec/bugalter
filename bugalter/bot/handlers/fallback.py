"""Последний обработчик: человек написал в личке что-то непонятное. Подсказываем, а не молчим."""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from .. import ui

router = Router(name="fallback")


@router.message(StateFilter(None), F.chat.type == "private", F.text)
async def not_understood(message: Message, session, actor, notifier):
    from .panel import can_admin

    admin = can_admin(actor)
    lines = ["🤔 Не понял. Пользуйтесь кнопками внизу."]
    if actor.employee is not None:
        lines.append("Отчёт о работе пишите не здесь, а в своей рабочей группе.")
        lines.append("Попросить аванс: кнопка 💸 Попросить аванс.")
    if admin:
        lines.append("Настройки и всё остальное: кнопка ⚙️ Управление.")
    lines.append("Подробная подсказка: ❓ Помощь.")
    notifier.answer(message, "\n".join(lines), await ui.main_menu_for(session, actor, admin))
