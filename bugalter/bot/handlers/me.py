"""Личный кабинет сотрудника (только личка): работа, расчёт, выплаты, запрос выплаты."""

import datetime as dt
import uuid

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ...domain.dates import parse_range
from ...domain.errors import ValidationError
from ...domain.money import fmt_qty, parse_decimal
from ...domain.permissions import Perm
from ...services import access, ledger, org, payroll, reference, work
from ...services.work import STATUS_TITLES
from .. import ui
from ..ui import h
from .payroll import _statement_for

router = Router(name="me")


class RequestPayout(StatesGroup):
    amount = State()


def _me(actor, is_private: bool):
    if not is_private:
        raise ValidationError("Личные данные доступны только в личном чате с ботом")
    if actor.employee is None:
        raise ValidationError("Ваш Telegram не привязан к карточке сотрудника. Отправьте администратору /id")
    return actor.employee


@router.message(Command("my_work"))
@router.message(F.text == ui.BTN_MY_WORK)
async def my_work(message: Message, session, actor, settings, notifier, is_private, command: CommandObject = None):
    emp = _me(actor, is_private)
    arg = command.args if command and command.args else None
    today = settings.today()
    if arg in ("прошлый", "prev"):
        current = await ledger.period_for_date(session, today, settings)
        period = await ledger.period_for_date(session, current.date_from - dt.timedelta(days=1), settings)
        date_from, date_to = period.date_from, period.date_to
    elif arg:
        date_from, date_to = parse_range(arg, today)
    else:
        period = await ledger.period_for_date(session, today, settings)
        date_from, date_to = period.date_from, period.date_to
    rows = await work.list_entries(session, employee_id=emp.id, date_from=date_from, date_to=date_to)
    rows = [r for r in rows if r[0].status != "draft"]
    lines = [f"📊 <b>Моя работа {date_from:%d.%m}-{date_to:%d.%m.%Y}</b>"]
    totals: dict[tuple, list] = {}
    for entry, wt, _ in rows:
        if entry.status in ("rejected", "cancelled"):
            continue
        key = (wt.name, entry.unit_code, entry.currency, entry.status)
        bucket = totals.setdefault(key, [0, 0, 0])
        bucket[0] += 1
        bucket[1] += entry.quantity
        bucket[2] += entry.amount
    for (name, unit, cur, status), (count, qty, amount) in sorted(totals.items()):
        lines.append(
            f"• {h(name)}: {fmt_qty(qty, unit)} ({count} оп.) = {await ui.money(session, amount, cur)}, "
            f"{STATUS_TITLES[status]}"
        )
    if not totals:
        lines.append("Операций нет.")
    lines.append("\n<u>Последние операции</u>")
    for entry, wt, _ in rows[:15]:
        lines.append(
            f"{ui.STATUS_ICONS[entry.status]} {entry.number} {entry.performed_on:%d.%m} {h(wt.name)} × "
            f"{fmt_qty(entry.quantity, entry.unit_code)} = {await ui.money(session, entry.amount, entry.currency)}"
            + (f" #{h(entry.order_ref)}" if entry.order_ref else "")
        )
    lines.append("\nДругой период: /my_work прошлый | месяц | 01.10-15.10")
    notifier.answer(message, "\n".join(lines))


@router.message(Command("my_salary"))
@router.message(F.text == ui.BTN_MY_SALARY)
async def my_salary(message: Message, session, actor, settings, notifier, is_private, command: CommandObject = None):
    emp = _me(actor, is_private)
    arg = command.args.strip() if command and command.args else None
    st = await _statement_for(session, settings, emp, arg)
    notifier.answer(message, ui.statement_text(st, with_available=True) + "\n\nПрошлый период: /my_salary прошлый")


@router.message(Command("my_payouts"))
@router.message(F.text == ui.BTN_MY_PAYOUTS)
async def my_payouts(message: Message, session, actor, notifier, is_private):
    emp = _me(actor, is_private)
    rows = await payroll.employee_payouts(session, emp.id)
    lines = ["🧾 <b>Мои выплаты</b>"]
    for p in rows:
        lines.append(
            f"• {p.number} {p.business_date:%d.%m.%Y} {await ui.money(session, p.amount, p.currency)}, "
            f"{payroll.PAYOUT_KIND_TITLES[p.kind]}, {payroll.PAYOUT_STATUS_TITLES[p.status]}"
        )
    if len(lines) == 1:
        lines.append("Выплат пока не было.")
    notifier.answer(message, "\n".join(lines))


async def _create_request(message, session, actor, settings, notifier, amount_text: str, comment: str | None, key: str):
    items = amount_text.split()
    amount = parse_decimal(items[0], what="сумму")
    currency = None
    if len(items) > 1 and items[1].upper() in await reference.currency_codes(session):
        currency = items[1].upper()
        items = items[2:]
    else:
        items = items[1:]
    comment = comment or (" ".join(items) or None)
    req, created = await payroll.request_payout(
        session, actor, settings, amount=amount, currency=currency, comment=comment, idempotency_key=key
    )
    kb = InlineKeyboardBuilder()
    if req.status == "requested":
        kb.button(text="🚫 Отменить запрос", callback_data=f"rq:x:{req.id}")
    notifier.answer(message, "Запрос отправлен:\n" + await ui.request_card(session, req), kb.as_markup())
    if created:
        dept = await org.default_department_id(session, actor.employee.id)
        card = await ui.request_card(session, req)
        for user in await access.users_with_perm(session, Perm.PAYROLL_PAYOUT, dept):
            notifier.send(user.telegram_id, "Новый запрос выплаты:\n" + card, ui.request_kb(req))


@router.message(Command("request_payout"))
async def request_payout_cmd(message: Message, command: CommandObject, session, actor, settings, notifier, is_private):
    _me(actor, is_private)
    if not command.args:
        raise ValidationError("Формат: /request_payout <сумма> [валюта] [комментарий]")
    await _create_request(
        message, session, actor, settings, notifier, command.args, None, f"msg:{message.chat.id}:{message.message_id}"
    )


@router.message(F.text == ui.BTN_REQUEST)
async def request_payout_button(message: Message, state: FSMContext, session, actor, settings, notifier, is_private):
    emp = _me(actor, is_private)
    lines = []
    for code in (await ledger.balances(session, emp.id)).keys() or [settings.currency]:
        available = await ledger.available(session, emp.id, code)
        line = await ui.money(session, max(available, 0), code)
        if available < 0:
            line += f" (долг {await ui.money(session, -available, code)})"
        credit = await payroll.credit_limit(session, settings, emp.id, code)
        if credit:
            line += f", аванс в долг до {await ui.money(session, credit, code)} сверх этого"
        lines.append(line)
    await state.set_state(RequestPayout.amount)
    await state.update_data(key=f"req:{uuid.uuid4().hex}")
    notifier.answer(
        message,
        "Доступно к выплате: " + ", ".join(lines) + "\nНапишите сумму (и при желании комментарий). /cancel: отмена",
    )


@router.message(RequestPayout.amount, F.text)
async def request_payout_amount(message: Message, state: FSMContext, session, actor, settings, notifier):
    data = await state.get_data()
    await state.clear()
    await _create_request(message, session, actor, settings, notifier, message.text, None, data["key"])
