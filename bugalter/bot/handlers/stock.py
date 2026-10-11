"""Склад: команды работают в рабочей группе (подразделение группы) или в личке с @подразделением."""

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from ...db.models import Employee, Location, Product, StockDocument, User
from ...domain.errors import AccessDenied, ValidationError
from ...domain.money import fmt_number, fmt_qty, parse_decimal
from ...domain.permissions import Perm
from ...services import access, org, reference, stock
from .. import ui
from ..args import extract, need, parse_id, parse_tail, resolve_department, tokens
from ..hooks import remember_card
from ..ui import h

router = Router(name="stock")


def _key(message: Message) -> str:
    return f"msg:{message.chat.id}:{message.message_id}"


async def _qty_unit(session, items: list[str]) -> tuple:
    """«3 уп …» → (3, 'уп', остаток)."""
    need(items, 1, "… <количество> [единица]")
    qty = parse_decimal(items[0], what="количество")
    rest = items[1:]
    unit = None
    if rest:
        codes = {u.code for u in await reference.list_units(session)}
        if rest[0] in codes:
            unit = rest.pop(0)
    return qty, unit, rest


async def _send_card(message, session, notifier, sessionmaker, result, is_private):
    doc = result.document
    card = await ui.stock_doc_card(session, doc)
    hook = remember_card(sessionmaker, StockDocument, doc.id) if result.created and not is_private else None
    notifier.answer(message, card, ui.stock_doc_kb(doc), on_sent=hook)
    return card


async def _ask_recipient(session, notifier, doc: StockDocument, card: str):
    """Получателю — карточка с кнопками «Получено/Не получено» в личку."""
    if doc.target_employee_id:
        emp = await session.get(Employee, doc.target_employee_id)
        if emp.user_id:
            user = await session.get(User, emp.user_id)
            notifier.send(user.telegram_id, "Вам передан товар. Подтвердите получение:\n" + card, ui.stock_doc_kb(doc))


@router.message(Command("receive"))
async def receive(
    message: Message, command: CommandObject, session, actor, chat_dept, notifier, is_private, sessionmaker
):
    ex = extract(tokens(command.args))
    need(ex.rest, 2, "/receive <артикул> <кол-во> [единица] [комментарий]")
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.STOCK_OPERATE)
    product = await stock.get_product(session, ex.rest[0])
    qty, unit, rest = await _qty_unit(session, ex.rest[1:])
    result = await stock.receive(
        session,
        actor,
        key=_key(message),
        department=dept,
        product=product,
        qty=qty,
        unit=unit,
        comment=" ".join(rest) or None,
        chat_id=None if is_private else message.chat.id,
    )
    await _send_card(message, session, notifier, sessionmaker, result, is_private)


@router.message(Command("issue"))
async def issue(
    message: Message, command: CommandObject, session, actor, chat_dept, notifier, is_private, sessionmaker
):
    ex = extract(tokens(command.args))
    need(ex.rest, 3, "/issue <сотрудник> <артикул> <кол-во> [единица] [комментарий]")
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.STOCK_OPERATE)
    emp = await org.get_employee(session, ex.rest[0])
    product = await stock.get_product(session, ex.rest[1])
    qty, unit, rest = await _qty_unit(session, ex.rest[2:])
    result = await stock.issue(
        session,
        actor,
        key=_key(message),
        department=dept,
        employee=emp,
        product=product,
        qty=qty,
        unit=unit,
        comment=" ".join(rest) or None,
        chat_id=None if is_private else message.chat.id,
    )
    card = await _send_card(message, session, notifier, sessionmaker, result, is_private)
    if result.created:
        await _ask_recipient(session, notifier, result.document, card)


@router.message(Command("move"))
async def move(message: Message, command: CommandObject, session, actor, chat_dept, notifier, is_private, sessionmaker):
    ex = extract(tokens(command.args))
    need(ex.rest, 4, "/move <от кого> <кому> <артикул> <кол-во> [единица]")
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.STOCK_OPERATE)
    src = await org.get_employee(session, ex.rest[0])
    dst = await org.get_employee(session, ex.rest[1])
    product = await stock.get_product(session, ex.rest[2])
    qty, unit, rest = await _qty_unit(session, ex.rest[3:])
    result = await stock.transfer(
        session,
        actor,
        key=_key(message),
        department=dept,
        from_employee=src,
        to_employee=dst,
        product=product,
        qty=qty,
        unit=unit,
        comment=" ".join(rest) or None,
        chat_id=None if is_private else message.chat.id,
    )
    card = await _send_card(message, session, notifier, sessionmaker, result, is_private)
    if result.created:
        await _ask_recipient(session, notifier, result.document, card)


@router.message(Command("send"))
async def send(message: Message, command: CommandObject, session, actor, chat_dept, notifier, is_private, sessionmaker):
    ex = extract(tokens(command.args))
    need(ex.rest, 3, "/send <подразделение-получатель> <артикул> <кол-во> [единица]")
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.STOCK_OPERATE)
    target = await org.get_department(session, ex.rest[0])
    product = await stock.get_product(session, ex.rest[1])
    qty, unit, rest = await _qty_unit(session, ex.rest[2:])
    result = await stock.send(
        session,
        actor,
        key=_key(message),
        department=dept,
        target=target,
        product=product,
        qty=qty,
        unit=unit,
        comment=" ".join(rest) or None,
        chat_id=None if is_private else message.chat.id,
    )
    card = await _send_card(message, session, notifier, sessionmaker, result, is_private)
    if result.created:
        for chat_id in await org.department_chats(session, target.id):
            notifier.send(
                chat_id, "Входящее перемещение. Подтвердите получение:\n" + card, ui.stock_doc_kb(result.document)
            )


@router.message(Command("return"))
async def return_cmd(
    message: Message, command: CommandObject, session, actor, chat_dept, notifier, is_private, sessionmaker
):
    ex = extract(tokens(command.args))
    need(ex.rest, 3, "/return <сотрудник> <артикул> <кол-во> [единица]")
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.STOCK_OPERATE)
    emp = await org.get_employee(session, ex.rest[0])
    product = await stock.get_product(session, ex.rest[1])
    qty, unit, rest = await _qty_unit(session, ex.rest[2:])
    result = await stock.return_to_stock(
        session,
        actor,
        key=_key(message),
        department=dept,
        employee=emp,
        product=product,
        qty=qty,
        unit=unit,
        comment=" ".join(rest) or None,
        chat_id=None if is_private else message.chat.id,
    )
    await _send_card(message, session, notifier, sessionmaker, result, is_private)


@router.message(Command("sold"))
async def sold(message: Message, command: CommandObject, session, actor, chat_dept, notifier, is_private, sessionmaker):
    ex = extract(tokens(command.args))
    need(ex.rest, 3, "/sold <сотрудник> <артикул> <кол-во> [единица] [#заказ] [комментарий]")
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.STOCK_OPERATE)
    emp = await org.get_employee(session, ex.rest[0])
    product = await stock.get_product(session, ex.rest[1])
    qty, unit, rest = await _qty_unit(session, ex.rest[2:])
    tail = parse_tail(rest)
    result = await stock.deliver(
        session,
        actor,
        key=_key(message),
        department=dept,
        employee=emp,
        product=product,
        qty=qty,
        unit=unit,
        order_ref=tail.order_ref,
        comment=tail.comment,
        chat_id=None if is_private else message.chat.id,
    )
    await _send_card(message, session, notifier, sessionmaker, result, is_private)


@router.message(Command("writeoff"))
async def writeoff(
    message: Message, command: CommandObject, session, actor, chat_dept, notifier, is_private, sessionmaker
):
    ex = extract(tokens(command.args), ("emp",))
    need(ex.rest, 3, "/writeoff <артикул> <кол-во> [единица] <причина> [emp=код — списать с сотрудника]")
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.STOCK_OPERATE)
    product = await stock.get_product(session, ex.rest[0])
    qty, unit, rest = await _qty_unit(session, ex.rest[1:])
    emp = await org.get_employee(session, ex.options["emp"]) if ex.options.get("emp") else None
    result = await stock.write_off(
        session,
        actor,
        key=_key(message),
        department=dept,
        product=product,
        qty=qty,
        unit=unit,
        reason=" ".join(rest),
        employee=emp,
        chat_id=None if is_private else message.chat.id,
    )
    await _send_card(message, session, notifier, sessionmaker, result, is_private)


@router.message(Command("inv"))
async def inventory(message: Message, command: CommandObject, session, actor, chat_dept, notifier, is_private):
    ex = extract(tokens(command.args))
    need(ex.rest, 3, "/inv <склад|сотрудник> <артикул> <фактическое кол-во> [единица]")
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.STOCK_INVENTORY)
    emp = None if ex.rest[0].lower() in ("склад", "warehouse") else await org.get_employee(session, ex.rest[0])
    product = await stock.get_product(session, ex.rest[1])
    counted = parse_decimal(ex.rest[2], allow_zero=True, what="количество")
    unit = ex.rest[3] if len(ex.rest) > 3 else None
    res = await stock.inventory(
        session,
        actor,
        key=_key(message),
        department=dept,
        product=product,
        counted=counted,
        unit=unit,
        employee=emp,
        chat_id=None if is_private else message.chat.id,
    )
    where = emp.full_name if emp else f"склад {dept.code}"
    text = (
        f"📋 <b>{res.document.number}</b> · Инвентаризация · {h(where)}\n{h(product.name)}: "
        f"по учёту {fmt_qty(res.expected, product.base_unit)}, по факту {fmt_qty(res.counted, product.base_unit)}"
    )
    if res.case:
        kind = "Недостача" if res.case.kind == "shortage" else "Излишек"
        text += (
            f"\n⚠️ {kind} {fmt_qty(res.case.qty, product.base_unit)} — расхождение <b>{res.case.number}</b> "
            "ждёт решения. Денежных последствий пока нет.\n"
            f"Решение: /case {res.case.id} company  или  "
            f"/case {res.case.id} employee &lt;цена&gt; [USD] &lt;основание&gt;"
        )
        if res.created:
            for user in await access.users_with_perm(session, Perm.STOCK_CASE_DECIDE, dept.id):
                if user.id != actor.user.id:
                    notifier.send(user.telegram_id, text)
    else:
        text += "\n✅ Расхождений нет."
    notifier.answer(message, text)


@router.message(Command("case"))
async def case(message: Message, command: CommandObject, session, actor, settings, notifier):
    items = tokens(command.args)
    need(items, 2, "/case <№> company | /case <№> employee <цена за баз. единицу> [USD] <основание цены>")
    case_id = parse_id(items[0])
    decision = items[1].lower()
    price = currency = basis = None
    if decision == "employee":
        need(items, 4, "/case <№> employee <цена> [USD] <основание цены>")
        price = parse_decimal(items[2], what="цену")
        rest = items[3:]
        if rest and rest[0].upper() in await reference.currency_codes(session):
            currency = rest.pop(0).upper()
        basis = " ".join(rest)
    result, adjustment = await stock.decide_case(
        session,
        actor,
        settings,
        case_id,
        decision=decision,
        price=price,
        currency=currency,
        price_basis=basis,
    )
    if adjustment is None:
        notifier.answer(message, f"✅ {result.number}: списано за счёт компании.")
        return
    card = await ui.adjustment_card(session, adjustment)
    notifier.answer(
        message,
        f"✅ {result.number}: отнесено на сотрудника. Создан документ удержания:\n{card}",
        ui.adjustment_kb(adjustment),
    )
    for user in await access.users_with_perm(session, Perm.PAYROLL_SIGN, adjustment.department_id):
        if user.id != actor.user.id:
            notifier.send(user.telegram_id, "На подпись:\n" + card, ui.adjustment_kb(adjustment))


@router.message(Command("cases"))
async def cases(message: Message, command: CommandObject, session, actor, chat_dept, notifier):
    ex = extract(tokens(command.args))
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.STOCK_CASE_DECIDE)
    rows = await stock.open_cases(session, dept.id)
    lines = []
    for c in rows:
        loc = await stock.describe_location(session, await session.get(Location, c.location_id))
        product = await session.get(Product, c.product_id)
        kind = "недостача" if c.kind == "shortage" else "излишек"
        lines.append(
            f"• <b>{c.number}</b> (/case {c.id}) {kind} {h(product.name)} "
            f"{fmt_qty(c.qty, product.base_unit)} · {h(loc)}"
        )
    notifier.answer(message, "\n".join(lines) or "Открытых расхождений нет.")


@router.message(Command("stock"))
async def stock_balances(message: Message, command: CommandObject, session, actor, chat_dept, notifier):
    ex = extract(tokens(command.args))
    dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.STOCK_OPERATE)
    if not (actor.can(Perm.STOCK_OPERATE, dept.id) or actor.can(Perm.REPORT_VIEW, dept.id)):
        raise AccessDenied("Недостаточно прав")
    emp = await org.get_employee(session, ex.rest[0]) if ex.rest else None
    rows = await stock.balances(session, department_id=dept.id, employee_id=emp.id if emp else None)
    lines = [f"📦 <b>Остатки {h(dept.code)}</b>" + (f" · {h(emp.full_name)}" if emp else "")]
    current = None
    for row in rows:
        if row.location_title != current:
            current = row.location_title
            lines.append(f"\n<u>{h(current)}</u>")
        lines.append(f"• {h(row.product.name)}: {fmt_qty(row.qty, row.product.base_unit)}")
    if len(lines) == 1:
        lines.append("Пусто.")
    notifier.answer(message, "\n".join(lines))


@router.message(Command("transit"))
async def transit(message: Message, session, actor, notifier):
    docs = await stock.pending_transfers(session, actor)
    if not docs:
        notifier.answer(message, "Нет товара в пути, который вы можете принять.")
        return
    for doc in docs[:15]:
        notifier.answer(message, await ui.stock_doc_card(session, doc), ui.stock_doc_kb(doc))


@router.callback_query(F.data.startswith("sd:"))
async def stock_doc_action(callback: CallbackQuery, session, actor, notifier):
    _, action, doc_id = callback.data.split(":")
    if action == "a":
        doc, changed = await stock.accept(session, actor, int(doc_id))
        notifier.toast(callback, "Получение подтверждено" if changed else "Уже подтверждено")
    else:
        doc, changed = await stock.decline(session, actor, int(doc_id), "получатель не принял")
        notifier.toast(callback, "Товар возвращён отправителю" if changed else "Уже обработано")
    card = await ui.stock_doc_card(session, doc)
    notifier.edit(callback.message, card, ui.stock_doc_kb(doc))
    if not changed:
        return
    # Обновляем карточку в группе (если нажали в личке) и сообщаем операторам — по требованию заказчика.
    if doc.chat_id and doc.message_id and doc.chat_id != callback.message.chat.id:
        notifier.edit_in_chat(doc.chat_id, doc.message_id, card)
    dept_id = doc.target_department_id or doc.department_id
    verb = "📥 Товар получен" if doc.status == "completed" else "↩️ Товар не принят"
    who = await ui.user_name(session, actor.user.id)
    for user in await access.users_with_perm(session, Perm.STOCK_RECEIPT_CONFIRM, dept_id):
        if user.id != actor.user.id:
            notifier.send(user.telegram_id, f"{verb} ({h(who)}):\n{card}")


# ------------------------------------------------------------------ номенклатура


@router.message(Command("products"))
async def products(message: Message, session, notifier):
    rows = await stock.list_products(session)
    lines = []
    for p, units in rows:
        alt = ", ".join(f"1 {u.unit_code} = {fmt_number(u.factor)} {p.base_unit}" for u in units)
        lines.append(f"• <code>{h(p.sku)}</code> {h(p.name)} · {h(p.base_unit)}" + (f" · {alt}" if alt else ""))
    notifier.answer(message, "\n".join(lines) or "Товаров нет. /product_add <артикул> <единица> <название>")


@router.message(Command("product_add"))
async def product_add(message: Message, command: CommandObject, session, actor, notifier):
    items = tokens(command.args)
    need(items, 3, "/product_add <артикул> <единица> <название>")
    p = await stock.create_product(session, actor, items[0], items[1], " ".join(items[2:]))
    notifier.answer(message, f"✅ Товар {h(p.name)} ({h(p.sku)}), базовая единица {h(p.base_unit)}")


@router.message(Command("product_unit"))
async def product_unit(message: Message, command: CommandObject, session, actor, notifier):
    items = tokens(command.args)
    need(items, 3, "/product_unit <артикул> <единица> <сколько базовых единиц в ней>")
    p = await stock.get_product(session, items[0])
    factor = parse_decimal(items[2], what="коэффициент")
    await stock.add_product_unit(session, actor, p, items[1], factor)
    notifier.answer(message, f"✅ 1 {h(items[1])} = {fmt_number(factor)} {h(p.base_unit)} для «{h(p.name)}»")


@router.message(Command("my_stock"))
@router.message(F.text == ui.BTN_MY_STOCK)
async def my_stock(message: Message, session, actor, notifier, is_private):
    if not is_private:
        raise ValidationError("Откройте личный чат с ботом")
    if actor.employee is None:
        raise ValidationError("Ваш Telegram не привязан к карточке сотрудника")
    rows = await stock.balances(session, employee_id=actor.employee.id)
    lines = ["📦 <b>Товар на руках</b>"]
    for row in rows:
        lines.append(f"• {h(row.product.name)}: {fmt_qty(row.qty, row.product.base_unit)} ({h(row.location_title)})")
    if len(lines) == 1:
        lines.append("Ничего не числится.")
    pending = [d for d in await stock.pending_transfers(session, actor) if actor.is_self(d.target_employee_id or 0)]
    if pending:
        lines.append(f"\n🚚 Ждут вашего подтверждения: {len(pending)} — /transit")
    notifier.answer(message, "\n".join(lines))
