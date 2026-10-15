"""Доплаты: коэффициенты, которые умножают сумму операции (срочно ×1,5, выходной ×2)."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from ....domain.errors import NotFound, ValidationError
from ....domain.money import fmt_number, parse_decimal
from ....domain.permissions import Perm
from ....services import tariffs
from ...ui import h
from .base import ask, cancel_kb, form, kb, show

router = Router(name="panel.coefficients")


async def coefs_view(session):
    lines = [
        "⚡ <b>Коэффициенты</b>",
        "Умножают сумму операции. В группе пишутся со звёздочкой: <code>/op ivan доставка 3 *срочно</code>",
        "",
    ]
    rows = []
    for coef in await tariffs.list_coefficients(session):
        state = "" if coef.is_active else " (выключен)"
        lines.append(f"• <b>*{h(coef.code)}</b> ×{fmt_number(coef.factor)}: {h(coef.name)}{state}")
        rows.append([(("⏸ " if coef.is_active else "▶️ ") + f"*{coef.code}", f"p:c:tg:{coef.code}")])
    rows.append([("➕ Новый коэффициент", "p:c:add")])
    rows.append([("⬅️ Назад", "p:more")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "p:c")
async def coefs(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    show(callback, notifier, *await coefs_view(session))


@router.callback_query(F.data.startswith("p:c:tg:"))
async def coef_toggle(callback: CallbackQuery, session, actor, notifier):
    code = callback.data.split(":", 3)[3]
    current = next((c for c in await tariffs.list_coefficients(session) if c.code == code), None)
    if current is None:
        raise NotFound("Коэффициент не найден")
    await tariffs.set_coefficient_active(session, actor, code, not current.is_active)
    await session.flush()
    show(callback, notifier, *await coefs_view(session))


@router.callback_query(F.data == "p:c:add")
async def coef_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    actor.require(Perm.TARIFF_MANAGE)
    await ask(state, "coef")
    show(
        callback,
        notifier,
        "✏️ Напишите слово и множитель, например: <b>срочно 1.5</b> или <b>выходной 2</b>",
        cancel_kb("p:c"),
    )


@form("coef")
async def form_coef(message, state, session, actor, notifier, **_):
    items = message.text.split()
    if len(items) < 2:
        raise ValidationError("Нужно слово и множитель: срочно 1.5")
    factor = parse_decimal(items[-1], max_scale=4, what="множитель")
    code = items[0].lower().lstrip("*")
    await tariffs.create_coefficient(session, actor, code, factor, " ".join(items[:-1]))
    await state.clear()
    await session.flush()
    notifier.answer(message, *await coefs_view(session))
