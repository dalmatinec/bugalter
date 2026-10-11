"""Операции в рабочей группе: ввод, подтверждение, отклонение, отмена, сводки."""

import datetime as dt
import uuid
from collections import defaultdict
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ...db.models import Employee, Unit, User, WorkEntry, WorkType
from ...domain.dates import parse_range
from ...domain.errors import ValidationError
from ...domain.money import fmt_qty, parse_decimal
from ...domain.permissions import Perm
from ...services import access, org, tariffs, work
from ...services.work import STATUS_TITLES
from .. import ui
from ..args import extract, need, parse_id, parse_tail, resolve_department, tail_date, tokens
from ..hooks import remember_card
from ..ui import h

router = Router(name="work")


class OpWizard(StatesGroup):
    employee = State()
    work_type = State()
    quantity = State()


def _show_amounts(settings, is_private: bool) -> bool:
    return is_private or settings.group_show_amounts


async def _notify_confirmers(session, notifier, entry: WorkEntry, actor, chat_id: int | None):
    """Если операцию ввели в личке — сообщаем руководителям; в группе карточка с кнопками уже видна."""
    if chat_id is not None:
        return
    text = await ui.entry_card(session, entry, show_amount=True)
    for user in await access.users_with_perm(session, Perm.WORK_CONFIRM, entry.department_id):
        if user.id != actor.user.id:
            notifier.send(user.telegram_id, "Новая операция на подтверждение:\n" + text, ui.entry_kb(entry))


async def _notify_employee(session, notifier, entry: WorkEntry, text_prefix: str):
    emp = await session.get(Employee, entry.employee_id)
    if emp and emp.user_id:
        user = await session.get(User, emp.user_id)
        notifier.send(user.telegram_id, text_prefix + "\n" + await ui.entry_card(session, entry, show_amount=True))


# ------------------------------------------------------------------ ввод


@router.message(Command("op"))
async def op_command(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session,
    actor,
    settings,
    chat_dept,
    notifier,
    is_private,
    sessionmaker,
    chat_employee=None,
):
    ex = extract(tokens(command.args))
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.WORK_CREATE)
    actor.require(Perm.WORK_CREATE, dept.id)
    if ex.rest:
        items = ex.rest
        # В личной группе сотрудника его код можно не писать: /op доставка 3
        if chat_employee is not None and await org.get_employee(session, items[0], required=False) is None:
            items = [chat_employee.code, *items]
        await _quick_op(message, items, dept, session, actor, settings, notifier, is_private, sessionmaker)
        return
    await state.clear()
    if chat_employee is not None:
        await state.update_data(dept_id=dept.id, key=f"wiz:{uuid.uuid4().hex}")
        await _ask_work_type(notifier, message, None, state, session, chat_employee)
        return
    employees = await org.department_employees(session, dept.id)
    if not employees:
        raise ValidationError(f"В подразделении «{dept.name}» нет сотрудников — добавьте их в /admin → Сотрудники")
    await state.set_state(OpWizard.employee)
    await state.update_data(dept_id=dept.id, key=f"wiz:{uuid.uuid4().hex}")
    kb = InlineKeyboardBuilder()
    for emp in employees:
        kb.button(text=emp.full_name, callback_data=f"opw:e:{emp.id}")
    kb.button(text="✖️ Отмена", callback_data="opw:x")
    kb.adjust(2)
    notifier.answer(message, f"🧾 Новая операция ({h(dept.code)}). Кто выполнил работу?", kb.as_markup())


async def _quick_op(message, items, dept, session, actor, settings, notifier, is_private, sessionmaker):
    need(items, 3, "/op <сотрудник> <работа> <кол-во> [#заказ] [вчера|ДД.ММ] [*коэффициент] [комментарий]")
    emp = await org.get_employee(session, items[0])
    wt = await tariffs.get_work_type(session, items[1])
    unit = await session.get(Unit, wt.unit_code)
    qty = parse_decimal(items[2], max_scale=unit.scale, what="количество")
    tail = parse_tail(items[3:])
    chat_id = None if is_private else message.chat.id
    result = await work.create_entry(
        session,
        actor,
        settings,
        idempotency_key=f"msg:{message.chat.id}:{message.message_id}",
        department_id=dept.id,
        employee_id=emp.id,
        work_type_id=wt.id,
        quantity=qty,
        performed_on=tail_date(tail, settings.today()),
        order_ref=tail.order_ref,
        comment=tail.comment,
        submit=True,
        chat_id=chat_id,
        coefficient_codes=tail.coefficients,
    )
    entry = result.entry
    card = await ui.entry_card(session, entry, _show_amounts(settings, is_private))
    hook = remember_card(sessionmaker, WorkEntry, entry.id) if result.created and chat_id else None
    notifier.answer(message, card, ui.entry_kb(entry), on_sent=hook)
    if result.created:
        await _notify_confirmers(session, notifier, entry, actor, chat_id)


@router.callback_query(OpWizard.employee, F.data.startswith("opw:e:"))
async def wiz_employee(callback: CallbackQuery, state: FSMContext, session, notifier):
    emp = await session.get(Employee, int(callback.data.split(":")[2]))
    await _ask_work_type(notifier, None, callback, state, session, emp)


async def _ask_work_type(notifier, message, callback, state: FSMContext, session, emp: Employee):
    data = await state.get_data()
    wts = await tariffs.work_types_for_department(session, data["dept_id"])
    if not wts:
        raise ValidationError("Нет доступных видов работ — добавьте их в /admin → Работы и ставки")
    await state.update_data(employee_id=emp.id)
    await state.set_state(OpWizard.work_type)
    kb = InlineKeyboardBuilder()
    for wt in wts:
        kb.button(text=wt.name, callback_data=f"opw:t:{wt.id}")
    kb.button(text="✖️ Отмена", callback_data="opw:x")
    kb.adjust(2)
    text = f"🧾 Новая операция · 👤 {h(emp.full_name)}\nКакая работа?"
    if callback is not None:
        notifier.edit(callback.message, text, kb.as_markup())
    else:
        notifier.answer(message, text, kb.as_markup())


@router.callback_query(OpWizard.work_type, F.data.startswith("opw:t:"))
async def wiz_work_type(callback: CallbackQuery, state: FSMContext, session, notifier):
    wt = await session.get(WorkType, int(callback.data.split(":")[2]))
    data = await state.get_data()
    emp = await session.get(Employee, data["employee_id"])
    await state.update_data(work_type_id=wt.id)
    await state.set_state(OpWizard.quantity)
    notifier.edit(callback.message, f"👤 {h(emp.full_name)} · 🛠 {h(wt.name)}")
    # ForceReply: в группе с включённым privacy mode бот получает только команды и ответы на свои сообщения.
    mention = f'<a href="tg://user?id={callback.from_user.id}">{h(callback.from_user.full_name)}</a>'
    notifier.send(
        callback.message.chat.id,
        f"{mention}, ответьте на это сообщение: количество в «{h(wt.unit_code)}».\n"
        "Можно добавить номер заказа, дату, коэффициент и комментарий: <code>3 #A-1052 вчера *срочно текст</code>\n"
        "/cancel — отмена",
        ForceReply(selective=True, input_field_placeholder="3 #A-1052"),
    )


@router.message(OpWizard.quantity, F.text)
async def wiz_quantity(message: Message, state: FSMContext, session, actor, settings, notifier, is_private):
    data = await state.get_data()
    wt = await session.get(WorkType, data["work_type_id"])
    unit = await session.get(Unit, wt.unit_code)
    items = message.text.split()
    qty = parse_decimal(items[0], max_scale=unit.scale, what="количество")
    tail = parse_tail(items[1:])
    chat_id = None if is_private else message.chat.id
    result = await work.create_entry(
        session,
        actor,
        settings,
        idempotency_key=data["key"],
        department_id=data["dept_id"],
        employee_id=data["employee_id"],
        work_type_id=wt.id,
        quantity=qty,
        performed_on=tail_date(tail, settings.today()),
        order_ref=tail.order_ref,
        comment=tail.comment,
        submit=False,
        chat_id=chat_id,
        coefficient_codes=tail.coefficients,
    )
    await state.clear()
    card = await ui.entry_card(session, result.entry, _show_amounts(settings, is_private))
    notifier.answer(message, "Проверьте и отправьте:\n" + card, ui.entry_kb(result.entry))


@router.callback_query(F.data == "opw:x")
async def wiz_cancel(callback: CallbackQuery, state: FSMContext, notifier):
    await state.clear()
    notifier.edit(callback.message, "Ввод операции отменён.")


@router.callback_query(F.data.startswith("opw:"))
async def wiz_foreign(callback: CallbackQuery, notifier):
    notifier.toast(callback, "Это меню открыл другой пользователь или оно устарело. Начните заново: /op", alert=True)


# ------------------------------------------------------------------ кнопки карточки


@router.callback_query(F.data.startswith("w:"))
async def entry_action(callback: CallbackQuery, session, actor, settings, notifier, is_private):
    parts = callback.data.split(":")
    action, entry_id = parts[1], int(parts[2])
    show = _show_amounts(settings, is_private)
    if action == "s":
        entry, changed = await work.submit_entry(session, actor, entry_id)
        notifier.toast(callback, "Отправлено на подтверждение" if changed else "Уже отправлено")
        if changed:
            entry.message_id = callback.message.message_id
            entry.chat_id = entry.chat_id or (None if is_private else callback.message.chat.id)
            await _notify_confirmers(session, notifier, entry, actor, entry.chat_id)
    elif action == "d":
        day = settings.today() - dt.timedelta(days=int(parts[3]))
        entry = await work.update_draft(session, actor, settings, entry_id, performed_on=day)
        notifier.toast(callback, f"Дата: {day:%d.%m.%Y}")
    elif action == "x":
        entry, _ = await work.cancel_entry(session, actor, settings, entry_id, "черновик отменён автором")
        notifier.toast(callback, "Отменено")
    elif action == "c":
        entry, posted, changed = await work.confirm_entry(session, actor, settings, entry_id)
        notifier.toast(callback, "Подтверждено, начислено" if changed else "Уже подтверждено")
        if changed:
            await _notify_employee(session, notifier, entry, "✅ Операция подтверждена:")
    elif action == "r":
        entry = await work.reject_entry(session, actor, settings, entry_id, None)
        notifier.toast(callback, "Отклонено")
        await _notify_employee(session, notifier, entry, "❌ Операция отклонена:")
    else:
        return
    card = await ui.entry_card(session, entry, show)
    notifier.edit(callback.message, card, ui.entry_kb(entry))


@router.message(Command("cancel_op", "reject_op"))
async def cancel_or_reject(message: Message, command: CommandObject, session, actor, settings, notifier, is_private):
    items = tokens(command.args)
    need(items, 2, f"/{command.command} <номер> <причина>")
    entry_id = parse_id(items[0])
    reason = " ".join(items[1:])
    if command.command == "cancel_op":
        entry, reversal = await work.cancel_entry(session, actor, settings, entry_id, reason)
        prefix = "🚫 Операция отменена" + (" (создано сторно)" if reversal else "")
    else:
        entry = await work.reject_entry(session, actor, settings, entry_id, reason)
        prefix = "❌ Операция отклонена"
    show = _show_amounts(settings, is_private)
    notifier.answer(message, prefix + "\n" + await ui.entry_card(session, entry, show))
    group_card = await ui.entry_card(session, entry, settings.group_show_amounts)
    notifier.edit_in_chat(entry.chat_id, entry.message_id, group_card)
    await _notify_employee(session, notifier, entry, prefix + ":")


@router.message(Command("op_info"))
async def op_info(message: Message, command: CommandObject, session, actor, settings, notifier, is_private):
    items = tokens(command.args)
    need(items, 1, "/op_info <номер>")
    entry = await work.get_entry(session, parse_id(items[0]))
    if not (actor.can(Perm.WORK_VIEW, entry.department_id) or actor.is_self(entry.employee_id)):
        raise ValidationError("Операция не найдена")
    card = await ui.entry_card(session, entry, _show_amounts(settings, is_private))
    notifier.answer(message, card, ui.entry_kb(entry))


@router.message(Command("pending"))
async def pending(message: Message, command: CommandObject, session, actor, settings, chat_dept, notifier, is_private):
    ex = extract(tokens(command.args))
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.WORK_VIEW)
    actor.require(Perm.WORK_VIEW, dept.id)
    rows = await work.list_entries(session, department_id=dept.id, statuses=("pending",), limit=15)
    if not rows:
        notifier.answer(message, "Нет операций, ожидающих подтверждения.")
        return
    show = _show_amounts(settings, is_private)
    for entry, _, _ in reversed(rows):
        notifier.answer(message, await ui.entry_card(session, entry, show), ui.entry_kb(entry))


@router.message(Command("ops"))
async def ops_summary(
    message: Message, command: CommandObject, session, actor, settings, chat_dept, notifier, is_private
):
    ex = extract(tokens(command.args))
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.WORK_VIEW)
    actor.require(Perm.WORK_VIEW, dept.id)
    date_from, date_to = parse_range(ex.rest[0] if ex.rest else None, settings.today())
    rows = await work.list_entries(session, department_id=dept.id, date_from=date_from, date_to=date_to)
    show = _show_amounts(settings, is_private)
    summary: dict[str, dict[tuple[str, str], list]] = defaultdict(lambda: defaultdict(lambda: [0, Decimal(0)]))
    statuses: dict[str, int] = defaultdict(int)
    money: dict[str, Decimal] = defaultdict(Decimal)
    for entry, wt, emp in rows:
        if entry.status == "draft":
            continue
        statuses[entry.status] += 1
        if entry.status in ("rejected", "cancelled"):
            continue
        bucket = summary[emp.full_name][(wt.name, entry.unit_code)]
        bucket[0] += 1
        bucket[1] += entry.quantity
        money[entry.currency] += entry.amount if entry.status == "confirmed" else 0
    period = f"{date_from:%d.%m}" + (f"–{date_to:%d.%m}" if date_to != date_from else "")
    lines = [f"📋 <b>Операции {h(dept.code)} за {period}</b>"]
    for name, items in sorted(summary.items()):
        lines.append(f"👤 {h(name)}")
        for (wt_name, unit), (count, qty) in sorted(items.items()):
            lines.append(f"   • {h(wt_name)}: {fmt_qty(qty, unit)} ({count} оп.)")
    if not summary:
        lines.append("Операций нет.")
    lines.append(" · ".join(f"{STATUS_TITLES[s]}: {c}" for s, c in statuses.items()))
    if show and money:
        totals = [await ui.money(session, v, k) for k, v in money.items()]
        lines.append("Подтверждено на сумму: " + ", ".join(totals))
    notifier.answer(message, "\n".join(lines))
