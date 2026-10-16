"""Отчёт о работе тегами в рабочей группе.

Сотрудник пишет в своей группе «#доставка 3» или несколько тегов сразу, либо столбиком:
название работы, под ним строки «5х05» (5 штук по 0,5). Бот находит работу по тегу,
считает по цене из Работ и цен и присылает одну карточку на весь отчёт с кнопками для подтверждения.
В группе сотрудника работа записывается на него, кто бы ни написал (он сам, оператор или владелец).
В общей группе - на того, кто написал, если он сотрудник.
"""

import re
from decimal import Decimal

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from ...db.models import Employee, StockDocument, Unit, User, WorkEntry, WorkType
from ...domain.dates import parse_date
from ...domain.errors import AccessDenied, ValidationError
from ...domain.money import fmt_number, fmt_qty, parse_decimal
from ...domain.permissions import Perm
from ...domain.tags import parse_block_report, parse_goods_list, parse_tag_report
from ...services import org, stock, tariffs, work
from ...services.work import STATUS_TITLES
from .. import ui
from ..ui import h

router = Router(name="tags")

# Работа с такой единицей считается по весу: «Крам факт 0.5 13» = 13 шт × 0,5 по цене за 1 единицу.
WEIGHT_UNITS = {"кг", "г", "л"}


def _product_title(text: str | None) -> str | None:
    """«крам» и «Крам» это один товар: храним с большой буквы, без лишних пробелов."""
    if not text:
        return None
    text = " ".join(text.split())
    return text[:1].upper() + text[1:]


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
    kb = InlineKeyboardBuilder()
    if any(e.status == "pending" for e in entries):
        kb.button(text="✅ Подтвердить", callback_data=f"tb:c:{chat_id}:{message_id}")
        kb.button(text="❌ Отклонить", callback_data=f"tb:r:{chat_id}:{message_id}")
        # Сотрудник ошибся и отправил не то: сам удаляет, пока не подтвердили.
        kb.button(text="🗑 Ошибся, удалить", callback_data=f"tb:x:{chat_id}:{message_id}")
        kb.adjust(2, 1)
    elif any(e.status == "confirmed" for e in entries):
        # Подтвердили по ошибке: отменяет руководитель или владелец, деньги снимаются сторно.
        kb.button(text="↩️ Отменить подтверждение", callback_data=f"tb:u:{chat_id}:{message_id}")
    else:
        return None
    return kb.as_markup()


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text.regexp(r"^\s*#\w"))
async def tag_report(message: Message, session, actor, settings, chat_dept, chat_employee, notifier):
    if chat_dept is None:
        return
    first, _, rest = message.text.strip().partition("\n")
    if first.split()[0].lower() in ACCEPT_COMMANDS:
        await _goods_preview(message, rest, session, actor, chat_dept, chat_employee, notifier)
        return
    # «#фасовка крам 1 40»: работа, товар, вес, штук. Если так не читается, то старый вид «#доставка 3».
    known = await _known(session)
    stripped = "\n".join(line.strip().removeprefix("#") for line in message.text.splitlines())
    report = parse_block_report(stripped, known)
    if not any(item.tag in known for item in report.items):
        report = parse_tag_report(message.text)
    await _save_report(message, report, session, actor, settings, chat_dept, chat_employee, notifier)


async def _known(session) -> dict[str, bool]:
    by_tag = await tariffs.work_types_by_tag(session)
    return {tag: wt.unit_code in WEIGHT_UNITS for tag, wt in by_tag.items()}


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text, ~F.text.startswith("/"))
async def block_report(message: Message, session, actor, settings, chat_dept, chat_employee, notifier):
    """Без решёток: отчёт о работе «Крам факт 0.5 13» или список сданного товара «Крам 05 13»."""
    if chat_dept is None:
        return
    known = await _known(session)
    report = parse_block_report(message.text, known)
    if any(item.tag in known for item in report.items):
        await _save_report(message, report, session, actor, settings, chat_dept, chat_employee, notifier)


# ------------------------------------------------------------------ приём готового товара оператором

# Первая строка сообщения оператора; дальше список «Крам 05 13». Сотрудник так не пишет, бот реагирует
# только на эту команду и только от того, у кого есть право на склад.
ACCEPT_COMMANDS = {"#принял", "#приём", "#прием", "#принято"}

_GOODS_LINE = re.compile(r"^• (.+) × (\d+(?:[.,]\d+)?) шт$")


async def _goods_preview(message, text, session, actor, chat_dept, chat_employee, notifier):
    """Оператор написал «#принял» и список. Сначала показываем, что поняли, и записываем только по кнопке."""
    if not actor.can(Perm.STOCK_OPERATE, chat_dept.id):
        return  # не оператор: молчим
    if chat_employee is None:
        raise ValidationError("#принял пишите в группе сотрудника, тогда понятно, от кого товар")
    if actor.is_self(chat_employee.id):
        raise AccessDenied("Свой товар на склад принимает другой человек")
    lines, other = parse_goods_list(text)
    if not lines:
        raise ValidationError(
            "После #принял с новой строки перечислите товар, вес и сколько штук:\n#принял\nКрам 05 13\nКрам 1 7"
        )
    resolved = [(line, await stock.resolve_finished(session, line.name, line.weight)) for line in lines]
    unknown = [line.text for line, g in resolved if g is None] + other
    if unknown:
        raise ValidationError(
            "Не понял: " + "; ".join(unknown) + ". Товар должен быть в списке Товары (Управление, Ещё, Товары). "
            "Исправьте и отправьте заново, ничего не записано"
        )
    out = [f"📦 <b>Принять от {h(chat_employee.full_name)}:</b>"]
    new = []
    for line, g in resolved:
        out.append(f"• {h(g.title)} × {h(line.qty)} шт")
        if g.product is None:
            new.append(g.title)
    if new:
        out.append("🆕 Новый товар заведётся сам: " + ", ".join(h(t) for t in new))
    out.append("Проверьте. Пока не нажали Записать, ничего не записано.")
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Записать на склад", callback_data=f"fg:a:{chat_employee.id}")
    kb.button(text="✖️ Отмена", callback_data=f"fg:r:{chat_employee.id}")
    kb.adjust(2)
    notifier.answer(message, "\n".join(out), kb.as_markup())


def _head(callback) -> str:
    return callback.message.html_text.split("\nПроверьте.")[0].split("\n✅ Записано")[0]


@router.callback_query(F.data.startswith("fg:"))
async def goods_action(callback: CallbackQuery, session, actor, settings, chat_dept, notifier):
    _, action, emp_id = callback.data.split(":")
    employee = await session.get(Employee, int(emp_id))
    dept = chat_dept
    if dept is None:
        notifier.toast(callback, "Группа не подключена", alert=True)
        return
    if not actor.can(Perm.STOCK_OPERATE, dept.id):
        raise AccessDenied("Принимает на склад оператор или кладовщик")
    if actor.is_self(employee.id):
        raise AccessDenied("Свой товар на склад принимает другой человек")
    who = await ui.actor_label(session, actor.user.id)
    head = _head(callback)
    key = f"fg:{callback.message.chat.id}:{callback.message.message_id}"
    if action == "r":
        notifier.edit(callback.message, head + f"\n✖️ Отменено ({h(who)}), ничего не записано")
        notifier.toast(callback, "Отменено")
        return
    if action == "u":
        await _undo_goods(session, actor, dept, employee, key)
        notifier.edit(callback.message, head + f"\n↩️ Приём отменён ({h(who)}), товар снят со склада")
        notifier.toast(callback, "Приём отменён")
        if employee.user_id:
            user = await session.get(User, employee.user_id)
            notifier.send(user.telegram_id, "↩️ Приём товара отменён:\n" + head.split("\n", 1)[-1])
        return
    items = [m.groups() for m in map(_GOODS_LINE.match, callback.message.text.splitlines()) if m]
    for index, (title, qty) in enumerate(items):
        name, _, weight = title.rpartition(" ")
        goods = await stock.resolve_finished(session, name, weight) if name else None
        if goods is None or goods.title != title:
            goods = await stock.resolve_finished(session, title, None)
        if goods is None:
            continue
        product = await stock.ensure_finished(session, actor, goods, dept.id)
        await stock.receive(
            session,
            actor,
            key=f"{key}:{index}",
            department=dept,
            product=product,
            qty=parse_decimal(qty, what="количество"),
            from_employee=employee,
            comment=f"готовое от {employee.full_name}",
            received_on=settings.today(),
        )
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Отменить приём (ошибка)", callback_data=f"fg:u:{employee.id}")
    notifier.edit(callback.message, head + f"\n✅ Записано на склад ({h(who)})", kb.as_markup())
    notifier.toast(callback, "Записано на склад")
    if employee.user_id:
        user = await session.get(User, employee.user_id)
        notifier.send(user.telegram_id, "📦 Склад принял у вас:\n" + head.split("\n", 1)[-1])


async def _undo_goods(session, actor, dept, employee, key: str) -> None:
    """Ошибочный приём: списываем те же количества со склада. История остаётся, видно и приём, и отмену."""
    docs = (
        (
            await session.execute(
                select(StockDocument).where(StockDocument.idempotency_key.like(f"{key}:%")).order_by(StockDocument.id)
            )
        )
        .scalars()
        .all()
    )
    if not docs:
        raise ValidationError("Нечего отменять")
    for doc in docs:
        for line, product in await stock.document_lines(session, doc.id):
            await stock.write_off(
                session,
                actor,
                key=f"undo:{doc.idempotency_key}",
                department=dept,
                product=product,
                qty=line.qty_base,
                reason=f"отмена ошибочного приёма {doc.number} от {employee.full_name}",
            )


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
            stats = {"pieces": pieces, "piece_weight": weight}
        else:
            qty = parse_decimal(item.qty, max_scale=unit.scale, what=f"количество для {what}")
            comment = " · ".join(filter(None, [item.product, report.comment])) or None
            stats = {"pieces": qty if unit.scale == 0 else None, "piece_weight": None}
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
                product_name=_product_title(item.product),
                **stats,
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
        if action == "u":
            if entry.status == "confirmed":
                await work.cancel_entry(session, actor, settings, entry.id, "подтверждено по ошибке")
                changed = True
            continue
        if entry.status != "pending":
            continue
        if action == "c":
            _, _, done = await work.confirm_entry(session, actor, settings, entry.id)
            changed = changed or done
        elif action == "x":
            if not (actor.is_self(entry.employee_id) or entry.created_by == actor.user.id):
                raise AccessDenied("Удалить может тот, кто отправил. Оператор нажимает Отклонить")
            await work.cancel_entry(session, actor, settings, entry.id, "сотрудник ошибся")
            changed = True
        else:
            await work.reject_entry(session, actor, settings, entry.id, None)
            changed = True
    entries = await work.entries_by_key_prefix(session, f"tag:{chat_id}:{message_id}:")
    done_text = {"c": "Подтверждено, начислено", "r": "Отклонено", "x": "Удалено", "u": "Подтверждение отменено"}
    notifier.toast(callback, done_text[action] if changed else "Уже решено")
    card = await report_card(session, entries, settings.group_show_amounts, [])
    notifier.edit(callback.message, card, report_kb(int(chat_id), int(message_id), entries))
    if changed:
        emp = await session.get(Employee, entries[0].employee_id)
        if emp.user_id and emp.user_id != actor.user.id:
            user = await session.get(User, emp.user_id)
            verdict = {
                "c": "✅ Отчёт подтверждён",
                "r": "❌ Отчёт отклонён",
                "x": "🗑 Отчёт удалён",
                "u": "↩️ Подтверждение отчёта отменено",
            }[action]
            notifier.send(user.telegram_id, verdict + ":\n" + await report_card(session, entries, True, []))
