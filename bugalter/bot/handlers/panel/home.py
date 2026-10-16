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

# Разделы панели: (кнопка, callback, строка описания, права, с любым из которых раздел виден).
HOME_SECTIONS = [
    ("👥 Сотрудники", "p:e", "добавить человека, бот даст ссылку для него", (Perm.ORG_MANAGE,)),
    ("💰 Работы и цены", "p:w", "за что платим и сколько", (Perm.TARIFF_MANAGE, Perm.WORKTYPE_MANAGE)),
    ("💸 Выплаты", "p:pay", "аванс и зарплата, просьбы об авансе", (Perm.PAYROLL_PAYOUT,)),
    ("📦 Склад", "p:st", "приход, выдача сотрудникам, остатки", (Perm.STOCK_OPERATE,)),
    ("📊 Отчёты", "p:r", "за Срок 1, Срок 2, месяц, квартал, в Excel", (Perm.REPORT_EXPORT,)),
    ("💬 Группы", "p:g", "как подключить рабочую группу", (Perm.ORG_MANAGE,)),
]

MORE_SECTIONS = [
    ("⚡ Доплаты", "p:c", "коэффициенты, например срочно ×1,5 или выходной ×2", (Perm.TARIFF_MANAGE,)),
    ("🏷 Товары", "p:pr", "список товаров и фасовки для склада", (Perm.STOCK_MANAGE,)),
    ("⚙️ Настройки", "p:s", "валюта, часовой пояс, периоды выплат, бэкап", (Perm.SETTINGS_MANAGE,)),
    ("🔑 Доступ", "p:a", "кто что может (обычно хватает выбора при добавлении)", (Perm.ACCESS_MANAGE,)),
    ("🏢 Филиалы", "p:d", "только если у вас несколько точек", (Perm.ORG_MANAGE,)),
    ("📜 Журнал", "p:j", "кто что менял", (Perm.SETTINGS_MANAGE,)),
]


def _visible(actor, sections):
    return [s for s in sections if actor is None or actor.is_owner or any(actor.can_anywhere(p) for p in s[3])]


def _rows(items, tail):
    buttons = [(title, data) for title, data, _hint, _perms in items]
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)] + [tail]


def _hints(items) -> list[str]:
    lines = []
    for title, _data, hint, _perms in items:
        icon, name = title.split(" ", 1)
        lines.append(f"{icon} <b>{name}</b>: {hint}")
    return lines


def home_text(actor=None) -> str:
    lines = ["⚙️ <b>Управление</b>", ""]
    lines += _hints(_visible(actor, HOME_SECTIONS))
    if _visible(actor, MORE_SECTIONS):
        lines += ["", "Остальное в разделе Ещё."]
    return "\n".join(lines)


def home_markup(actor=None) -> InlineKeyboardMarkup:
    tail = [("➕ Ещё", "p:more")] if _visible(actor, MORE_SECTIONS) else []
    return kb(_rows(_visible(actor, HOME_SECTIONS), tail))


def more_text(actor=None) -> str:
    lines = ["➕ <b>Ещё</b>", ""]
    lines += _hints(_visible(actor, MORE_SECTIONS))
    return "\n".join(lines)


def more_markup(actor=None) -> InlineKeyboardMarkup:
    return kb(_rows(_visible(actor, MORE_SECTIONS), [("⬅️ В меню", "p:home")]))


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
    notifier.answer(message, home_text(actor), home_markup(actor))


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
        f"✅ Валюта: {currency_label(code)} ({code}). Поменять можно в Ещё, Настройки.\n\n" + home_text(actor),
        home_markup(actor),
    )


@router.callback_query(F.data == "p:home")
async def home(callback: CallbackQuery, state: FSMContext, actor, notifier):
    await state.clear()
    show(callback, notifier, home_text(actor), home_markup(actor))


@router.callback_query(F.data == "p:more")
async def more(callback: CallbackQuery, state: FSMContext, actor, notifier):
    await state.clear()
    show(callback, notifier, more_text(actor), more_markup(actor))
