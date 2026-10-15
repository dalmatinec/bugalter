"""Деньги: премии/штрафы/удержания с подписью, выплаты, запросы, периоды. Только в личке с ботом."""

import datetime as dt

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ...db.models import Employee, User
from ...domain.dates import parse_range
from ...domain.errors import AccessDenied, ValidationError
from ...domain.money import fmt_money, parse_decimal
from ...domain.permissions import Perm
from ...services import access, ledger, org, payroll, reference
from .. import ui
from ..args import extract, need, parse_id, tokens
from ..ui import h

router = Router(name="payroll")

PAYOUT_KINDS = {
    "аванс": "advance",
    "advance": "advance",
    "зп": "salary",
    "зарплата": "salary",
    "salary": "salary",
    "запрос": "on_demand",
    "on_demand": "on_demand",
}


def _private(is_private: bool) -> None:
    if not is_private:
        raise ValidationError("Денежные операции выполняются только в личном чате с ботом")


async def _employee_tg(session, employee_id: int) -> int | None:
    emp = await session.get(Employee, employee_id)
    if emp and emp.user_id:
        return (await session.get(User, emp.user_id)).telegram_id
    return None


async def _amount_currency(session, items: list[str]):
    amount = parse_decimal(items[0], what="сумму")
    rest = items[1:]
    currency = None
    if rest and rest[0].upper() in await reference.currency_codes(session):
        currency = rest.pop(0).upper()
    return amount, currency, rest


# ------------------------------------------------------------------ премии, штрафы, удержания


@router.message(Command("bonus", "penalty", "deduct"))
async def create_adjustment(message: Message, command: CommandObject, session, actor, settings, notifier, is_private):
    _private(is_private)
    kind = {"bonus": "bonus", "penalty": "penalty", "deduct": "deduction"}[command.command]
    ex = extract(tokens(command.args))
    need(ex.rest, 3, f"/{command.command} <сотрудник> <сумма> [валюта] <основание>")
    emp = await org.get_employee(session, ex.rest[0])
    amount, currency, rest = await _amount_currency(session, ex.rest[1:])
    dept_id = (await org.get_department(session, ex.dept)).id if ex.dept else None
    adj, created = await payroll.create_adjustment(
        session,
        actor,
        settings,
        kind=kind,
        employee=emp,
        amount=amount,
        currency=currency,
        reason=" ".join(rest),
        idempotency_key=f"msg:{message.chat.id}:{message.message_id}",
        department_id=dept_id,
    )
    card = await ui.adjustment_card(session, adj)
    notifier.answer(message, card, ui.adjustment_kb(adj))
    if created:
        for user in await access.users_with_perm(session, Perm.PAYROLL_SIGN, adj.department_id):
            if user.id != actor.user.id:
                notifier.send(user.telegram_id, "На подпись:\n" + card, ui.adjustment_kb(adj))


@router.message(Command("adjs"))
async def open_adjustments(message: Message, session, actor, notifier, is_private):
    _private(is_private)
    rows = await payroll.open_adjustments(session, actor)
    if not rows:
        notifier.answer(message, "Документов на подпись нет.")
        return
    for adj, _ in rows[:20]:
        notifier.answer(message, await ui.adjustment_card(session, adj), ui.adjustment_kb(adj))


@router.callback_query(F.data.startswith("adj:"))
async def adjustment_action(callback: CallbackQuery, session, actor, settings, notifier):
    _, action, adj_id = callback.data.split(":")
    adj_id = int(adj_id)
    employee_tg = None
    if action == "s":
        adj, posted, changed = await payroll.sign_adjustment(session, actor, settings, adj_id)
        employee_tg = await _employee_tg(session, adj.employee_id) if changed else None
        if adj.status == "awaiting_consent":
            notifier.toast(callback, "Подписано. Ждём согласия сотрудника")
            if employee_tg:
                notifier.send(
                    employee_tg,
                    "Подтвердите согласие с документом:\n" + await ui.adjustment_card(session, adj),
                    ui.consent_kb(adj),
                )
            employee_tg = None
        else:
            notifier.toast(callback, "Подписано и проведено" if changed else "Уже подписано")
    elif action == "r":
        adj = await payroll.reject_adjustment(session, actor, adj_id, None)
        notifier.toast(callback, "Отклонено")
    elif action in ("y", "n"):
        adj, posted = await payroll.consent_adjustment(session, actor, settings, adj_id, action == "y")
        notifier.toast(callback, "Спасибо, ответ записан")
        for user in await access.users_with_perm(session, Perm.PAYROLL_SIGN, adj.department_id):
            notifier.send(
                user.telegram_id,
                ("✅ Сотрудник согласен" if action == "y" else "✋ Сотрудник не согласен")
                + ":\n"
                + await ui.adjustment_card(session, adj),
            )
    else:
        return
    card = await ui.adjustment_card(session, adj)
    notifier.edit(callback.message, card, ui.adjustment_kb(adj))
    if employee_tg and adj.status == "approved":
        notifier.send(employee_tg, "Вам проведён документ:\n" + card)


@router.message(Command("adj_cancel"))
async def adjustment_cancel(message: Message, command: CommandObject, session, actor, settings, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 2, "/adj_cancel <№> <причина>")
    adj, reversal = await payroll.cancel_adjustment(session, actor, settings, parse_id(items[0]), " ".join(items[1:]))
    note = " (создано сторно)" if reversal else ""
    notifier.answer(message, f"🚫 Отменено{note}\n" + await ui.adjustment_card(session, adj))


# ------------------------------------------------------------------ выплаты


@router.message(Command("payout"))
async def payout(message: Message, command: CommandObject, session, actor, settings, notifier, is_private):
    _private(is_private)
    ex = extract(tokens(command.args))
    need(ex.rest, 2, "/payout <сотрудник> <сумма> [валюта] [аванс|зп|запрос] [способ/комментарий]")
    emp = await org.get_employee(session, ex.rest[0])
    amount, currency, rest = await _amount_currency(session, ex.rest[1:])
    kind = "salary"
    if rest and rest[0].lower() in PAYOUT_KINDS:
        kind = PAYOUT_KINDS[rest.pop(0).lower()]
    dept_id = (await org.get_department(session, ex.dept)).id if ex.dept else None
    p, created = await payroll.register_payout(
        session,
        actor,
        settings,
        employee=emp,
        amount=amount,
        currency=currency,
        kind=kind,
        idempotency_key=f"msg:{message.chat.id}:{message.message_id}",
        comment=" ".join(rest) or None,
        department_id=dept_id,
    )
    card = await ui.payout_card(session, p)
    notifier.answer(message, card, ui.payout_kb(p))
    if not created:
        return
    if p.status == "registered":
        owner_only = settings.payout_confirm_owner_only
        for user in await access.deciders(session, Perm.PAYROLL_PAYOUT_CONFIRM, p.department_id, owner_only):
            if user.id != actor.user.id:
                notifier.send(user.telegram_id, "Выплата ждёт подтверждения:\n" + card, ui.payout_kb(p))
    elif (tg := await _employee_tg(session, p.employee_id)) is not None:
        notifier.send(tg, "💸 Вам проведена выплата:\n" + card)


@router.message(Command("payouts"))
async def payouts(message: Message, session, actor, notifier, is_private):
    _private(is_private)
    rows = await payroll.registered_payouts(session, actor)
    if not rows:
        notifier.answer(message, "Неподтверждённых выплат нет.")
        return
    for p, _ in rows[:20]:
        notifier.answer(message, await ui.payout_card(session, p), ui.payout_kb(p))


@router.callback_query(F.data.startswith("po:c:"))
async def payout_confirm(callback: CallbackQuery, session, actor, settings, notifier):
    p, _, changed = await payroll.confirm_payout(session, actor, settings, int(callback.data.split(":")[2]))
    notifier.toast(callback, "Выплата проведена" if changed else "Уже проведена")
    card = await ui.payout_card(session, p)
    notifier.edit(callback.message, card)
    if changed and (tg := await _employee_tg(session, p.employee_id)) is not None:
        notifier.send(tg, "💸 Вам проведена выплата:\n" + card)


@router.message(Command("payout_cancel"))
async def payout_cancel(message: Message, command: CommandObject, session, actor, settings, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 2, "/payout_cancel <№> <причина>")
    p, reversal = await payroll.cancel_payout(session, actor, settings, parse_id(items[0]), " ".join(items[1:]))
    notifier.answer(
        message,
        "🚫 Выплата отменена" + (" (создано сторно)" if reversal else "") + "\n" + await ui.payout_card(session, p),
    )


# ------------------------------------------------------------------ запросы выплат


@router.message(Command("requests"))
async def requests(message: Message, session, actor, notifier, is_private):
    _private(is_private)
    rows = await payroll.open_requests(session, actor)
    if not rows:
        notifier.answer(message, "Открытых запросов выплат нет.")
        return
    for req, _ in rows[:20]:
        notifier.answer(message, await ui.request_card(session, req), ui.request_kb(req))


@router.callback_query(F.data.startswith("rq:"))
async def request_action(callback: CallbackQuery, session, actor, settings, notifier):
    _, action, req_id = callback.data.split(":")
    if action == "x":
        req = await payroll.cancel_request(session, actor, int(req_id))
        notifier.toast(callback, "Запрос отменён")
        notifier.edit(callback.message, await ui.request_card(session, req))
        return
    req, p = await payroll.decide_request(session, actor, settings, int(req_id), action == "a")
    if p is not None:
        await payroll.confirm_if_allowed(session, actor, settings, p)
    notifier.toast(callback, "Одобрено" if req.status == "approved" else "Отклонено")
    text = await ui.request_card(session, req)
    markup = None
    if p is not None:
        text += "\n\n" + await ui.payout_card(session, p)
        markup = ui.payout_kb(p)
    notifier.edit(callback.message, text, markup)
    if (tg := await _employee_tg(session, req.employee_id)) is not None:
        notifier.send(tg, "Решение по вашему запросу:\n" + await ui.request_card(session, req))


# ------------------------------------------------------------------ расчёт и периоды


async def _statement_for(session, settings, employee, arg: str | None):
    today = settings.today()
    if not arg or arg in ("текущий", "current"):
        period = await ledger.period_for_date(session, today, settings)
        return await payroll.build_statement(session, employee, period=period, include_reserved=True)
    if arg in ("прошлый", "prev"):
        current = await ledger.period_for_date(session, today, settings)
        period = await ledger.period_for_date(session, current.date_from - dt.timedelta(days=1), settings)
        return await payroll.build_statement(session, employee, period=period)
    if arg.lower().startswith("п") and arg[1:].isdigit():
        return await payroll.build_statement(session, employee, period=await payroll.get_period(session, int(arg[1:])))
    date_from, date_to = parse_range(arg, today)
    return await payroll.build_statement(session, employee, date_from=date_from, date_to=date_to)


async def _can_view_employee(session, actor, employee) -> bool:
    if actor.is_self(employee.id) or actor.can(Perm.REPORT_VIEW):
        return True
    return any(actor.can(Perm.REPORT_VIEW, d) for d in await org.employee_department_ids(session, employee.id))


@router.message(Command("balance"))
async def balance(message: Message, command: CommandObject, session, actor, settings, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 1, "/balance <сотрудник> [прошлый|п12|месяц|01.10-15.10]")
    emp = await org.get_employee(session, items[0])
    if not await _can_view_employee(session, actor, emp):
        raise AccessDenied("Недостаточно прав")
    st = await _statement_for(session, settings, emp, items[1] if len(items) > 1 else None)
    notifier.answer(message, ui.statement_text(st, with_available=True))


@router.message(Command("periods"))
async def periods(message: Message, session, settings, notifier, is_private):
    _private(is_private)
    await ledger.period_for_date(session, settings.today(), settings)
    rows = await payroll.list_periods(session)
    lines = ["<b>Расчётные периоды</b>"]
    for p in rows:
        status = "🔒 закрыт" if p.status == "closed" else "🟢 открыт"
        lines.append(f"• п{p.id}: {p.title}, {status}" + (f", версия {p.version}" if p.version else ""))
    lines.append(
        "\nЗакрыть: /period_close &lt;№&gt; · итоги: /period &lt;№&gt; · расчёт: /balance &lt;код&gt; п&lt;№&gt;"
    )
    notifier.answer(message, "\n".join(lines))


def _force_kb(period_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔒 Закрыть всё равно", callback_data=f"pr:f:{period_id}")
    return kb.as_markup()


async def _close(session, actor, settings, period_id: int, force: bool) -> str:
    period, statements = await payroll.close_period(session, actor, settings, period_id, force=force)
    return (
        f"🔒 Период {period.title} закрыт (версия {period.version}). Зафиксированы итоги по "
        f"{len(statements)} сотрудникам: /period {period.id}"
    )


@router.message(Command("period_close"))
async def period_close(message: Message, command: CommandObject, session, actor, settings, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 1, "/period_close <№>")
    period_id = parse_id(items[0])
    try:
        text = await _close(session, actor, settings, period_id, force=False)
    except payroll.PendingEntriesError as exc:
        notifier.answer(
            message,
            f"⚠️ {exc}. Если закрыть сейчас, их подтверждение позже попадёт в следующий период "
            "(с исходной датой работы).",
            _force_kb(period_id),
        )
        return
    notifier.answer(message, text)


@router.callback_query(F.data.startswith("pr:f:"))
async def period_force_close(callback: CallbackQuery, session, actor, settings, notifier):
    text = await _close(session, actor, settings, int(callback.data.split(":")[2]), force=True)
    notifier.edit(callback.message, text)


@router.message(Command("period_reopen"))
async def period_reopen(message: Message, command: CommandObject, session, actor, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 2, "/period_reopen <№> <причина>")
    period = await payroll.reopen_period(session, actor, parse_id(items[0]), " ".join(items[1:]))
    notifier.answer(
        message,
        f"🔓 Период {period.title} переоткрыт. Причина записана в журнал. После исправлений закройте его снова, "
        "будет сохранена новая версия итогов, старая останется.",
    )


@router.message(Command("period"))
async def period_info(message: Message, command: CommandObject, session, actor, notifier, is_private):
    _private(is_private)
    if not actor.can_anywhere(Perm.REPORT_VIEW):
        raise AccessDenied("Недостаточно прав")
    items = tokens(command.args)
    need(items, 1, "/period <№>")
    period, rows = await payroll.period_statements(session, parse_id(items[0]))
    lines = [f"<b>Итоги периода {period.title}</b> (версия {period.version})"]
    if not rows:
        lines.append("Период не закрывался или проводок не было.")
    for st, emp in rows:
        m = await ui.minor_units(session, st.currency)
        lines.append(
            f"• {h(emp.full_name)}: вход {fmt_money(st.opening, st.currency, m)}, начислено "
            f"{fmt_money(st.accrued, st.currency, m)}, удержано {fmt_money(st.deducted, st.currency, m)}, "
            f"корр. {fmt_money(st.corrections, st.currency, m)}, выплачено {fmt_money(st.paid, st.currency, m)} → "
            f"<b>{fmt_money(st.closing, st.currency, m)}</b>"
        )
    notifier.answer(message, "\n".join(lines))
