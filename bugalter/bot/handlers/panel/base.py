"""Общие кусочки панели: клавиатуры, вопросы с ответом текстом, права на вход."""

from collections.abc import Awaitable, Callable
from decimal import Decimal

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from ....db.models import Role
from ....domain.errors import ValidationError
from ....domain.money import fmt_number, fmt_price
from ....domain.permissions import OWNER_ROLE, Perm

PAGE = 8

ADMIN_PERMS = (
    Perm.ORG_MANAGE,
    Perm.ACCESS_MANAGE,
    Perm.WORKTYPE_MANAGE,
    Perm.TARIFF_MANAGE,
    Perm.STOCK_MANAGE,
    Perm.SETTINGS_MANAGE,
    Perm.PAYROLL_SIGN,
)


class AdminForm(StatesGroup):
    input = State()


# Ответ текстом на вопрос панели: имя формы -> обработчик. Заполняется декоратором @form в модулях разделов.
FORMS: dict[str, Callable[..., Awaitable[None]]] = {}


def form(name: str):
    def register(handler):
        FORMS[name] = handler
        return handler

    return register


def can_admin(actor) -> bool:
    return actor.is_owner or any(actor.can_anywhere(p) for p in ADMIN_PERMS)


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*[_button(text, data) for text, data in row])
    return builder.as_markup()


def _button(text: str, data: str) -> InlineKeyboardButton:
    if data.startswith("http"):
        return InlineKeyboardButton(text=text, url=data)
    return InlineKeyboardButton(text=text, callback_data=data)


def cancel_kb(back: str) -> InlineKeyboardMarkup:
    return kb([[("✖️ Отмена", back)]])


def show(event, notifier, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(event, CallbackQuery):
        notifier.edit(event.message, text, markup)
    else:
        notifier.answer(event, text, markup)


def require_private(is_private: bool) -> None:
    if not is_private:
        raise ValidationError("Панель управления открывается в личном чате с ботом")


async def ask(state: FSMContext, form_name: str, **data) -> None:
    await state.set_state(AdminForm.input)
    await state.update_data(form=form_name, **data)


def rate_text(rate: Decimal, currency: str, method: str = "per_unit") -> str:
    if method == "percent":
        return f"{fmt_number(rate)} %"
    return fmt_price(rate, currency)


async def role_names(session) -> dict[str, str]:
    return dict((await session.execute(select(Role.code, Role.name))).all())


ROLES_HELP = (
    "• <b>Оператор</b>: вносит операции и складские движения в группе\n"
    "• <b>Руководитель</b>: ведёт операции, премии и штрафы, выплаты, ставки своего подразделения "
    "(подтверждает операции и выплаты только владелец)\n"
    "• <b>Кладовщик</b>: склад и инвентаризация\n"
    "• <b>Бухгалтер</b>: выплаты, премии, отчёты\n"
    "• <b>Администратор</b>: всё, кроме прав владельца\n"
    "Сотруднику без роли бот показывает только его работу и расчёт."
)


async def role_choice(session, prefix: str, back: str) -> InlineKeyboardMarkup:
    rows = []
    roles = (await session.execute(select(Role).order_by(Role.code))).scalars()
    for role in roles:
        if role.code == OWNER_ROLE:
            continue
        rows.append([(role.name, f"{prefix}:{role.code}")])
    rows.append([("⬅️ Назад", back)])
    return kb(rows)
