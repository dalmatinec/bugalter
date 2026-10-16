"""Отчёт о работе тегами в рабочей группе.

Сотрудник пишет в своей группе «#доставка 3» или несколько тегов сразу, либо столбиком:
название работы, под ним строки «5х05» (5 штук по 0,5). Бот находит работу по тегу,
считает по цене из Работ и цен и присылает одну карточку на весь отчёт с кнопками для подтверждения.
В группе сотрудника работа записывается на него, кто бы ни написал (он сам, оператор или владелец).
В общей группе - на того, кто написал, если он сотрудник.
"""

from decimal import Decimal

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ...db.models import Employee, Unit, User, WorkEntry, WorkType
from ...domain.dates import parse_date
from ...domain.errors import ValidationError
from ...domain.money import fmt_number, fmt_qty, parse_decimal
from ...domain.tags import parse_block_report, parse_tag_report
from ...services import org, tariffs, work
from ...services.work import STATUS_TITLES
from .. import ui
from ..ui import h

router = Router(name="tags")

# Работа с такой единицей считается по весу: «Крам факт 0.5 13» = 13 шт × 0,5 по цене за 1 единицу.
WEIGHT_UNITS = {"кг", "г", "л"}


def _weight(text: str) -> str:
    """«05» значит 0,5, «025» значит 0,25: так пишут вес без запятой."""
    text = text.replace(",", ".")
    if len(text) > 1 and text.startswith("0") and "." not in text:
        return "0." + text[1:]
    return text


async def _reporter(session, actor, chat_dept, chat_employee) -> Employee:
    if chat_employee is not None:
        return chat_employee
    if actor.employee is not None and await org.is_member(session, actor.employee.id, chat_dept.id):
        return actor.employee
    raise ValidationError("Отчёт тегами пишите в группе сотрудника, тогда понятно, чья это работа")


async def report_card(session, entries: list[WorkEntry], show_amount: bool, unknown: list[str]) -> str:
    emp = await session.get(Employee, entries[0].employee_id)
    lines = [f"🧾 <b>Отчёт: {h(emp.full_name)}</b> · {entries[0].performed_on:%d.%m.%Y}"]
    totals: dict[str, object] = {}
    for entry in entries:
        wt = await session.get(WorkType, entry.work_type_id)
        line = f"{ui.STATUS_ICONS[entry.status]} {h(wt.name)} × {fmt_qty(entry.quantity, entry.unit_code)}"
        if show_amount:
            line += f" = {await ui.money(session, entry.amount, entry.currency)}"
        if entry.comment and len({e.comment for e in entries}) > 1:
            line += f"\n      {h(entry.comment)}"
        lines.append(line)
        if entry.status in ("pending", "confirmed"):
            totals[entry.currency] = totals.get(entry.currency, 0) + entry.amount
    if show_amount and totals:
        lines.append("Итого: " + ", ".join([await ui.money(session, v, k) for k, v in totals.items()]))
    statuses = {e.status for e in entries}
    lines.append(" · ".join(STATUS_TITLES[s] for s in sorted(statuses)))
    if entries[0].comment and len({e.comment for e in entries}) == 1:
        lines.append(f"💬 {h(entries[0].comment)}")
    if unknown:
        names = ", ".join(h(t) for t in unknown)
        lines.append(f"❓ Не нашёл работу: {names}. Названия и теги видны в Управлении, Работы и цены.")
    lines.append(f"✍️ прислал: {h(await ui.actor_label(session, entries[0].created_by))}")
    return "\n".join(lines)


def report_kb(chat_id: int, message_id: int, entries: list[WorkEntry]) -> InlineKeyboardMarkup | None:
    if not any(e.status == "pending" for e in entries):
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"tb:c:{chat_id}:{message_id}")
    kb.button(text="❌ Отклонить", callback_data=f"tb:r:{chat_id}:{message_id}")
    kb.adjust(2)
    return kb.as_markup()


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text.regexp(r"^\s*#\w"))
async def tag_report(message: Message, session, actor, settings, chat_dept, chat_employee, notifier):
    report = parse_tag_report(message.text)
    await _save_report(message, report, session, actor, settings, chat_dept, chat_employee, notifier)


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text, ~F.text.startswith("/"))
async def block_report(message: Message, session, actor, settings, chat_dept, chat_employee, notifier):
    """Отчёт без решёток: «Крам факт 1 13» одной строкой или столбиком «Готовые крам», «5х05», «10х1»."""
    if chat_dept is None:
        return
    by_tag = await tariffs.work_types_by_tag(session)
    known = {tag: wt.unit_code in WEIGHT_UNITS for tag, wt in by_tag.items()}
    report = parse_block_report(message.text, known)
    await _save_report(message, report, session, actor, settings, chat_dept, chat_employee, notifier)


async def _save_report(message, report, session, actor, settings, chat_dept, chat_employee, notifier):
    if chat_dept is None or not report.items:
        return
    by_tag = await tariffs.work_types_by_tag(session)
    found = [(by_tag.get(tariffs.work_tag(item.tag)), item) for item in report.items]
    if all(wt is None for wt, _ in found):
        return  # обычная переписка или чужие хэштеги, не наш отчёт
    employee = await _reporter(session, actor, chat_dept, chat_employee)
    performed_on = parse_date(report.date_token, settings.today()) if report.date_token else None
    results, unknown = [], []
    for index, (wt, item) in enumerate(found):
        if wt is None:
            unknown.append(item.label or f"#{item.tag}")
            continue
        unit = await session.get(Unit, wt.unit_code)
        what = item.label or "#" + item.tag
        comment = report.comment
        if wt.unit_code in WEIGHT_UNITS and item.weight is not None:
            # Работа по весу: штуки × вес одной штуки, цена за 1 единицу веса.
            pieces = parse_decimal(item.qty, max_scale=0, what=f"количество штук для {what}")
            weight = parse_decimal(_weight(item.weight), what=f"вес для {what}")
            qty = pieces * weight
            if qty != qty.quantize(Decimal(1).scaleb(-unit.scale)):
                raise ValidationError(
                    f"{what}: получилось {fmt_number(qty)} {unit.code}, а {unit.code} считается до "
                    f"{unit.scale} знаков после запятой"
                )
            detail = f"{fmt_number(pieces)} шт по {fmt_number(weight)} {unit.code}"
            comment = " · ".join(filter(None, [item.product, detail, report.comment]))
        else:
            qty = parse_decimal(item.qty, max_scale=unit.scale, what=f"количество для {what}")
            comment = " · ".join(filter(None, [item.product, report.comment])) or None
        results.append(
            await work.create_entry(
                session,
                actor,
                settings,
                idempotency_key=f"tag:{message.chat.id}:{message.message_id}:{index}",
                department_id=chat_dept.id,
                employee_id=employee.id,
                work_type_id=wt.id,
                quantity=qty,
                performed_on=performed_on,
                comment=comment,
                submit=True,
                chat_id=message.chat.id,
                self_report=True,
            )
        )
    if not any(r.created for r in results):
        return  # Telegram прислал то же сообщение повторно
    entries = [r.entry for r in results]
    card = await report_card(session, entries, settings.group_show_amounts, unknown)
    notifier.answer(message, card, report_kb(message.chat.id, message.message_id, entries))


@router.callback_query(F.data.startswith("tb:"))
async def report_action(callback: CallbackQuery, session, actor, settings, notifier):
    _, action, chat_id, message_id = callback.data.split(":")
    entries = await work.entries_by_key_prefix(session, f"tag:{chat_id}:{message_id}:")
    if not entries:
        notifier.toast(callback, "Отчёт не найден", alert=True)
        return
    changed = False
    for entry in entries:
        if entry.status != "pending":
            continue
        if action == "c":
            _, _, done = await work.confirm_entry(session, actor, settings, entry.id)
            changed = changed or done
        else:
            await work.reject_entry(session, actor, settings, entry.id, None)
            changed = True
    entries = await work.entries_by_key_prefix(session, f"tag:{chat_id}:{message_id}:")
    notifier.toast(callback, ("Подтверждено, начислено" if action == "c" else "Отклонено") if changed else "Уже решено")
    card = await report_card(session, entries, settings.group_show_amounts, [])
    notifier.edit(callback.message, card, report_kb(int(chat_id), int(message_id), entries))
    if changed:
        emp = await session.get(Employee, entries[0].employee_id)
        if emp.user_id and emp.user_id != actor.user.id:
            user = await session.get(User, emp.user_id)
            verdict = "✅ Отчёт подтверждён" if action == "c" else "❌ Отчёт отклонён"
            notifier.send(user.telegram_id, verdict + ":\n" + await report_card(session, entries, True, []))
