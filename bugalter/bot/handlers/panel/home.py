"""Вход в панель и два главных экрана: Управление и Ещё."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ....db.models import Setting
from ....domain.errors import AccessDenied
from ....domain.money import currency_label
from ....domain.permissions import Perm
from ....services import reference
from ....services import settings as settings_service
from ... import ui
from .base import can_admin, kb, require_private, show

router = Router(name="panel.home")

HOME_TEXT = """⚙️ <b>Управление</b>

👥 <b>Сотрудники</b>: добавить человека, бот даст ссылку для него
💰 <b>Работы и цены</b>: за что платим и сколько
💸 <b>Выплаты</b>: аванс и зарплата, вычесть аванс или оставить на потом
💬 <b>Группы</b>: как подключить рабочую группу

Остальное (доплаты, склад, настройки, доступ) - в разделе Ещё."""


def home_markup() -> InlineKeyboardMarkup:
    return kb(
        [
            [("👥 Сотрудники", "p:e"), ("💰 Работы и цены", "p:w")],
            [("💸 Выплаты", "p:pay"), ("💬 Группы", "p:g")],
            [("➕ Ещё", "p:more")],
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
async def open_panel(message: Message, state: FSMContext, session, actor, notifier, is_private):
    require_private(is_private)
    if not can_admin(actor):
        raise AccessDenied("Управление доступно владельцу, администратору и руководителям")
    await state.clear()
    if actor.can(Perm.SETTINGS_MANAGE) and await session.get(Setting, "currency") is None:
        # Первый запуск: валюту выбираем до того, как заведены цены, чтобы все суммы были в ней.
        notifier.answer(message, "👋 Для начала: в какой валюте платите сотрудникам?", await _currency_kb(session))
        return
    notifier.answer(message, HOME_TEXT, home_markup())


async def _currency_kb(session) -> InlineKeyboardMarkup:
    buttons = [(f"{currency_label(c)} {c}", f"p:first:{c}") for c in sorted(await reference.currency_codes(session))]
    return kb([buttons[i : i + 3] for i in range(0, len(buttons), 3)])


@router.callback_query(F.data.startswith("p:first:"))
async def first_currency(callback: CallbackQuery, session, actor, notifier):
    code = callback.data.split(":")[2]
    await settings_service.set_setting(session, actor, "currency", code)
    show(
        callback,
        notifier,
        f"✅ Валюта: {currency_label(code)} ({code}). Поменять можно в Ещё, Настройки.\n\n" + HOME_TEXT,
        home_markup(),
    )


@router.callback_query(F.data == "p:home")
async def home(callback: CallbackQuery, state: FSMContext, notifier):
    await state.clear()
    show(callback, notifier, HOME_TEXT, home_markup())


@router.callback_query(F.data == "p:more")
async def more(callback: CallbackQuery, state: FSMContext, notifier):
    await state.clear()
    show(callback, notifier, MORE_TEXT, more_markup())
