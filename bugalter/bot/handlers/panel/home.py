"""Вход в панель и два главных экрана: Управление и Ещё."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ....domain.errors import AccessDenied
from ... import ui
from .base import can_admin, kb, require_private, show

router = Router(name="panel.home")

HOME_TEXT = """⚙️ <b>Управление</b>

👥 <b>Сотрудники</b>: добавить человека, бот даст ссылку для него
💰 <b>Работы и цены</b>: за что платим и сколько
💬 <b>Группы</b>: как подключить рабочую группу

Остальное (доплаты, склад, настройки, доступ) - в разделе Ещё."""


def home_markup() -> InlineKeyboardMarkup:
    return kb(
        [
            [("👥 Сотрудники", "p:e"), ("💰 Работы и цены", "p:w")],
            [("💬 Группы", "p:g"), ("➕ Ещё", "p:more")],
        ]
    )


MORE_TEXT = """➕ <b>Ещё</b>

⚡ <b>Доплаты</b>: коэффициенты, например срочно ×1,5 или выходной ×2
📦 <b>Товары</b>: если ведёте склад
⚙️ <b>Настройки</b>: валюта, часовой пояс, периоды выплат, лимиты
🔑 <b>Доступ</b>: кто что может (обычно хватает выбора при добавлении сотрудника)
🏢 <b>Филиалы</b>: только если у вас несколько точек со своими сотрудниками и складом
📜 <b>Журнал</b>: кто что менял"""


def more_markup() -> InlineKeyboardMarkup:
    return kb(
        [
            [("⚡ Доплаты", "p:c"), ("📦 Товары", "p:pr")],
            [("⚙️ Настройки", "p:s"), ("🔑 Доступ", "p:a")],
            [("🏢 Филиалы", "p:d"), ("📜 Журнал", "p:j")],
            [("⬅️ В меню", "p:home")],
        ]
    )


@router.message(Command("admin"))
@router.message(F.text == ui.BTN_PANEL)
async def open_panel(message: Message, state: FSMContext, actor, notifier, is_private):
    require_private(is_private)
    if not can_admin(actor):
        raise AccessDenied("Управление доступно владельцу, администратору и руководителям")
    await state.clear()
    notifier.answer(message, HOME_TEXT, home_markup())


@router.callback_query(F.data == "p:home")
async def home(callback: CallbackQuery, state: FSMContext, notifier):
    await state.clear()
    show(callback, notifier, HOME_TEXT, home_markup())


@router.callback_query(F.data == "p:more")
async def more(callback: CallbackQuery, state: FSMContext, notifier):
    await state.clear()
    show(callback, notifier, MORE_TEXT, more_markup())
