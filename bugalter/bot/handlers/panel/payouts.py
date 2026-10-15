"""Выплаты: аванс любой суммой и зарплата с вычетом аванса или без (тогда он вычтется в следующий раз)."""

import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from ....db.models import Employee, User
from ....domain.errors import NotFound
from ....domain.money import currency_label, fmt_money, parse_decimal
from ....domain.permissions import Perm
from ....services import access, org, payroll
from ... import ui
from ...ui import h
from .base import ask, cancel_kb, form, kb, show

router = Router(name="panel.payouts")


async def payouts_view(session, settings, actor):
    cur = settings.currency
    lines = [
        "💸 <b>Выплаты</b>",
        "Аванс выдаётся любой суммой. В день зарплаты решаете: вычесть аванс сейчас или в следующий раз.",
        "",
    ]
    rows = []
    requests = await payroll.open_requests(session, actor)
    if requests:
        rows.append([(f"🙋 Просят аванс: {len(requests)}", "p:pay:rq")])
    waiting = await payroll.registered_payouts(session, actor)
    if waiting:
        rows.append([(f"⏳ Ждут подтверждения: {len(waiting)}", "p:pay:po")])
    for emp, _ in await org.list_employees(session):
        if not emp.is_active:
            continue
        pv = await payroll.salary_preview(session, emp.id, cur)
        line = f"• <b>{h(emp.full_name)}</b>: заработано {fmt_money(pv.earned, cur)}"
        if pv.advance:
            line += f", аванс {fmt_money(pv.advance, cur)}"
        lines.append(line)
        rows.append([(f"💸 {emp.full_name}", f"p:pay:v:{emp.id}")])
    if len(lines) == 3:
        lines.append("Сотрудников пока нет.")
    rows.append([("⬅️ В меню", "p:home")])
    return "\n".join(lines), kb(rows)


async def employee_pay_view(session, settings, emp_id: int):
    emp = await session.get(Employee, emp_id)
    if emp is None:
        raise NotFound("Сотрудник не найден")
    cur = settings.currency
    pv = await payroll.salary_preview(session, emp.id, cur)
    lines = [f"💸 <b>{h(emp.full_name)}</b>", f"💰 Заработано, зарплатой не выплачено: {fmt_money(pv.earned, cur)}"]
    rows = [[("💵 Выдать аванс", f"p:pay:adv:{emp.id}")]]
    if pv.advance:
        lines.append(f"💵 Аванс, ещё не вычтен: {fmt_money(pv.advance, cur)}")
        if pv.earned > 0:
            left = f", остаток аванса {fmt_money(pv.advance_left, cur)} вычтется потом" if pv.advance_left else ""
            lines += [
                "",
                f"✅ С вычетом аванса: к выплате {fmt_money(pv.with_deduction, cur)}{left}",
                f"⏭ Без вычета: к выплате {fmt_money(pv.earned, cur)}, аванс вычтется в следующую зарплату",
            ]
            rows.append([(f"✅ С вычетом: {fmt_money(pv.with_deduction, cur)}", f"p:pay:s:{emp.id}:1")])
            rows.append([(f"⏭ Без вычета: {fmt_money(pv.earned, cur)}", f"p:pay:s:{emp.id}:0")])
    elif pv.earned > 0:
        rows.append([(f"💰 Выплатить зарплату: {fmt_money(pv.earned, cur)}", f"p:pay:s:{emp.id}:0")])
    rows.append([("⬅️ Выплаты", "p:pay")])
    return "\n".join(lines), kb(rows)


async def _finish(session, actor, settings, notifier, payout, employee: Employee, text: str) -> str:
    """Проводит выплату сразу, если записавший может её подтвердить; иначе зовёт тех, кто подтверждает."""
    if await payroll.confirm_if_allowed(session, actor, settings, payout):
        if employee.user_id:
            user = await session.get(User, employee.user_id)
            notifier.send(user.telegram_id, text)
        return "✅ Выдано."
    owner_only = settings.payout_confirm_owner_only
    card = await ui.payout_card(session, payout)
    for user in await access.deciders(session, Perm.PAYROLL_PAYOUT_CONFIRM, payout.department_id, owner_only):
        if user.id != actor.user.id:
            notifier.send(user.telegram_id, "Выплата ждёт подтверждения:\n" + card, ui.payout_kb(payout))
    return "⏳ Записано, ждёт подтверждения владельца."


@router.callback_query(F.data == "p:pay")
async def payouts(callback: CallbackQuery, state: FSMContext, session, actor, settings, notifier):
    await state.clear()
    show(callback, notifier, *await payouts_view(session, settings, actor))


@router.callback_query(F.data.startswith("p:pay:v:"))
async def employee_pay(callback: CallbackQuery, state: FSMContext, session, settings, notifier):
    await state.clear()
    show(callback, notifier, *await employee_pay_view(session, settings, int(callback.data.split(":")[3])))


@router.callback_query(F.data.startswith("p:pay:adv:"))
async def advance_ask(callback: CallbackQuery, state: FSMContext, session, actor, settings, notifier):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    actor.require(Perm.PAYROLL_PAYOUT, await org.default_department_id(session, emp.id))
    await ask(state, "advance", emp_id=emp.id, key=f"adv:{uuid.uuid4().hex}")
    show(
        callback,
        notifier,
        f"💵 Аванс: <b>{h(emp.full_name)}</b>\n"
        f"Сколько выдать, в {currency_label(settings.currency)}? Можно больше заработанного: "
        "потом в день зарплаты вычтете его кнопкой.",
        cancel_kb(f"p:pay:v:{emp.id}"),
    )


@form("advance")
async def form_advance(message, state, session, actor, settings, notifier, data, **_):
    emp = await session.get(Employee, data["emp_id"])
    amount = parse_decimal(message.text.split()[0], what="сумму")
    comment = " ".join(message.text.split()[1:]) or None
    payout, _ = await payroll.register_payout(
        session,
        actor,
        settings,
        employee=emp,
        amount=amount,
        currency=None,
        kind="advance",
        idempotency_key=data["key"],
        comment=comment,
    )
    await state.clear()
    money = fmt_money(amount, settings.currency)
    result = await _finish(
        session, actor, settings, notifier, payout, emp, f"💵 Вам выдан аванс {money}. Он вычтется из зарплаты."
    )
    await session.flush()
    text, markup = await employee_pay_view(session, settings, emp.id)
    notifier.answer(message, f"{result} Аванс {money}.\n\n{text}", markup)


@router.callback_query(F.data.startswith("p:pay:s:"))
async def salary_confirm(callback: CallbackQuery, session, settings, notifier):
    _, _, _, emp_id, deduct = callback.data.split(":")
    emp = await session.get(Employee, int(emp_id))
    cur = settings.currency
    pv = await payroll.salary_preview(session, emp.id, cur)
    if deduct == "1":
        amount = pv.with_deduction
        what = f"вычесть аванс {fmt_money(pv.deducted_now, cur)} и выплатить {fmt_money(amount, cur)}"
    else:
        amount = pv.earned
        what = f"выплатить {fmt_money(amount, cur)}" + (", аванс оставить на следующий раз" if pv.advance else "")
    nonce = uuid.uuid4().hex[:12]
    show(
        callback,
        notifier,
        f"💰 Зарплата: <b>{h(emp.full_name)}</b>\nТочно {what}?",
        kb([[("✅ Да", f"p:pay:ok:{emp.id}:{deduct}:{nonce}"), ("✖️ Нет", f"p:pay:v:{emp.id}")]]),
    )


@router.callback_query(F.data.startswith("p:pay:ok:"))
async def salary_pay(callback: CallbackQuery, session, actor, settings, notifier):
    _, _, _, emp_id, deduct, nonce = callback.data.split(":")
    emp = await session.get(Employee, int(emp_id))
    payout, deducted = await payroll.pay_salary(
        session, actor, settings, employee=emp, deduct_advance=deduct == "1", idempotency_key=f"salary:{nonce}"
    )
    cur = settings.currency
    note = f", вычтен аванс {fmt_money(deducted, cur)}" if deducted else ""
    if payout is None:
        result = f"✅ Аванс {fmt_money(deducted, cur)} вычтен, к выплате ничего."
        if emp.user_id:
            user = await session.get(User, emp.user_id)
            notifier.send(user.telegram_id, f"💵 Из заработанного вычтен аванс {fmt_money(deducted, cur)}.")
    else:
        text = f"💰 Вам выплачена зарплата {fmt_money(payout.amount, cur)}{note}."
        result = await _finish(session, actor, settings, notifier, payout, emp, text)
        result += f" Зарплата {fmt_money(payout.amount, cur)}{note}."
    await session.flush()
    text, markup = await employee_pay_view(session, settings, emp.id)
    show(callback, notifier, f"{result}\n\n{text}", markup)


@router.callback_query(F.data == "p:pay:rq")
async def advance_requests(callback: CallbackQuery, session, actor, settings, notifier):
    rows = []
    lines = ["🙋 <b>Просят аванс</b>", ""]
    for req, emp in await payroll.open_requests(session, actor):
        money = fmt_money(req.amount, req.currency)
        lines.append(f"• {h(emp.full_name)}: {money}" + (f", {h(req.comment)}" if req.comment else ""))
        rows.append([(f"✅ {emp.full_name}: {money}", f"rq:a:{req.id}"), ("❌", f"rq:r:{req.id}")])
    if not rows:
        lines.append("Сейчас никто не просит.")
    rows.append([("⬅️ Выплаты", "p:pay")])
    show(callback, notifier, "\n".join(lines), kb(rows))


@router.callback_query(F.data == "p:pay:po")
async def waiting_payouts(callback: CallbackQuery, session, actor, notifier):
    rows = []
    lines = ["⏳ <b>Ждут подтверждения</b>", ""]
    for payout, emp in await payroll.registered_payouts(session, actor):
        money = fmt_money(payout.amount, payout.currency)
        kind = payroll.PAYOUT_KIND_TITLES[payout.kind]
        lines.append(f"• {h(emp.full_name)}: {kind} {money}")
        rows.append([(f"✅ {emp.full_name}: {money}", f"po:c:{payout.id}")])
    if not rows:
        lines.append("Всё подтверждено.")
    rows.append([("⬅️ Выплаты", "p:pay")])
    show(callback, notifier, "\n".join(lines), kb(rows))
