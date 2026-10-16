"""Склад на кнопках: сколько пришло, сколько выдано какому сотруднику, сколько осталось.

Приход кладёт товар на склад. Выдача уходит сотруднику и ждёт, пока он нажмёт Получено.
Возврат снимает товар с сотрудника обратно на склад.
"""

import uuid
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy import func, select

from ....db.models import Department, Employee, Product, StockDocument, StockDocumentLine, User
from ....domain.errors import AccessDenied, ValidationError
from ....domain.money import fmt_number, fmt_qty, parse_decimal
from ....domain.permissions import Perm
from ....services import org, reference, stock
from ... import ui
from ...ui import h
from .base import ask, cancel_kb, form, kb, show

router = Router(name="panel.stock")


def _can_view(actor) -> None:
    if not (actor.is_owner or actor.can_anywhere(Perm.STOCK_OPERATE) or actor.can_anywhere(Perm.STOCK_MANAGE)):
        raise AccessDenied("Склад доступен владельцу, кладовщику и руководителю")


async def _totals(session, dept_id: int) -> dict[int, dict[str, Decimal]]:
    """Сколько всего пришло, выдано и вернули по каждому товару (непринятые выдачи не считаются)."""
    from_people = StockDocument.employee_id.isnot(None)
    rows = await session.execute(
        select(
            StockDocumentLine.product_id,
            StockDocument.doc_type,
            from_people,
            func.sum(StockDocumentLine.qty_base),
        )
        .join(StockDocument, StockDocument.id == StockDocumentLine.document_id)
        .where(
            StockDocument.department_id == dept_id,
            StockDocument.status != "returned",
            StockDocument.doc_type.in_(("receipt", "issue", "return", "writeoff")),
        )
        .group_by(StockDocumentLine.product_id, StockDocument.doc_type, from_people)
    )
    result: dict[int, dict[str, Decimal]] = {}
    for product_id, doc_type, by_employee, qty in rows.all():
        key = {("receipt", True): "made", ("writeoff", True): "used"}.get((doc_type, by_employee), doc_type)
        bucket = result.setdefault(product_id, {})
        bucket[key] = bucket.get(key, Decimal(0)) + qty
    return result


async def _places(session, dept_id: int) -> dict[int, dict]:
    """Остатки по товарам: склад, в пути, у каждого сотрудника."""
    result: dict[int, dict] = {}
    for row in await stock.balances(session, department_id=dept_id):
        place = result.setdefault(row.product.id, {"warehouse": Decimal(0), "transit": Decimal(0), "people": []})
        if row.location.kind == "employee":
            emp = await session.get(Employee, row.location.employee_id)
            place["people"].append((emp, row.qty))
        elif row.location.kind in ("warehouse", "transit"):
            place[row.location.kind] += row.qty
    return result


async def stock_view(session, actor, dept_id: int):
    dept = await session.get(Department, dept_id)
    title = "📦 <b>Склад</b>" + (f" · {h(dept.name)}" if await org.single_department(session) is None else "")
    lines = [title, ""]
    totals = await _totals(session, dept_id)
    places = await _places(session, dept_id)
    for product, _units in await stock.list_products(session):
        t = totals.get(product.id, {})
        p = places.get(product.id)
        if not t and not p:
            continue
        u = product.base_unit
        lines.append(f"<b>{h(product.name)}</b>")
        parts = [
            (title, t.get(key))
            for key, title in (
                ("receipt", "пришло"),
                ("made", "сдали готового"),
                ("issue", "выдано"),
                ("return", "вернули"),
                ("used", "израсходовано"),
                ("writeoff", "списано"),
            )
        ]
        lines.append(" · ".join(f"{title} {fmt_number(qty)}" for title, qty in parts if qty) + f" {h(u)}")
        lines.append(f"🏬 на складе: <b>{fmt_qty(p['warehouse'] if p else Decimal(0), u)}</b>")
        for emp, qty in (p or {}).get("people", []):
            lines.append(f"👤 {h(emp.full_name)}: {fmt_qty(qty, u)}")
        if p and p["transit"]:
            lines.append(f"🚚 выдано, ждёт подтверждения: {fmt_qty(p['transit'], u)}")
        lines.append("")
    if len(lines) == 2:
        lines.append("Движений пока нет. Начните с прихода товара на склад: кнопка 📥 Приход.")
        lines.append("Не знаете, с чего начать? Нажмите ❓ Как работать со складом.")
    rows = [
        [("📥 Приход", f"p:st:in:{dept_id}"), ("📤 Выдать", f"p:st:out:{dept_id}")],
        [("📦 Принять готовое", f"p:st:fin:{dept_id}"), ("✂️ Израсходовано", f"p:st:use:{dept_id}")],
        [("↩️ Возврат", f"p:st:ret:{dept_id}"), ("📜 История", f"p:st:log:{dept_id}")],
        [("❓ Как работать со складом", f"p:st:help:{dept_id}")],
    ]
    if actor.is_owner or actor.can_anywhere(Perm.STOCK_MANAGE):
        rows.append([("🏷 Товары", "p:pr")])
    rows.append([("⬅️ В меню", "p:home")])
    return "\n".join(lines).rstrip(), kb(rows)


@router.callback_query(F.data == "p:st")
async def stock_home(callback: CallbackQuery, state: FSMContext, session, actor, notifier):
    _can_view(actor)
    await state.clear()
    dept = await org.single_department(session)
    if dept is not None:
        show(callback, notifier, *await stock_view(session, actor, dept.id))
        return
    rows = [[(f"🏢 {d.name}", f"p:st:d:{d.id}")] for d in await org.active_departments(session)]
    rows.append([("⬅️ В меню", "p:home")])
    show(callback, notifier, "📦 Склад какого филиала?", kb(rows))


@router.callback_query(F.data.startswith("p:st:d:"))
async def stock_dept(callback: CallbackQuery, state: FSMContext, session, actor, notifier):
    _can_view(actor)
    await state.clear()
    show(callback, notifier, *await stock_view(session, actor, int(callback.data.split(":")[3])))


def _grid(buttons, back: str):
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([("⬅️ Склад", back)])
    return kb(rows)


async def _products_kb(session, dept_id: int, prefix: str, only: dict[int, Decimal] | None = None):
    places = await _places(session, dept_id)
    buttons = []
    for product, _units in await stock.list_products(session):
        if only is not None:
            if product.id not in only:
                continue
            have = only[product.id]
        else:
            have = places.get(product.id, {}).get("warehouse", Decimal(0))
        buttons.append((f"{product.name} ({fmt_number(have)} {product.base_unit})", f"{prefix}:{product.id}"))
    return buttons


# ------------------------------------------------------------------ приход


@router.callback_query(F.data.startswith("p:st:in:"))
async def receive_start(callback: CallbackQuery, session, actor, notifier):
    dept_id = int(callback.data.split(":")[3])
    actor.require(Perm.STOCK_OPERATE, dept_id)
    buttons = await _products_kb(session, dept_id, f"p:st:ip:{dept_id}")
    if not buttons:
        show(
            callback,
            notifier,
            "Товаров пока нет. Сначала добавьте товар.",
            kb([[("➕ Новый товар", "p:pr:add")], [("⬅️ Склад", f"p:st:d:{dept_id}")]]),
        )
        return
    show(callback, notifier, "📥 Какой товар пришёл?", _grid(buttons, f"p:st:d:{dept_id}"))


@router.callback_query(F.data.startswith("p:st:ip:"))
async def receive_product(callback: CallbackQuery, state: FSMContext, session, actor, notifier):
    _, _, _, dept_id, product_id = callback.data.split(":")
    product = await session.get(Product, int(product_id))
    await ask(state, "st_qty", op="in", dept_id=int(dept_id), product_id=product.id, key=_new_key())
    show(
        callback,
        notifier,
        f"📥 Сколько пришло <b>{h(product.name)}</b>? {await _qty_hint(session, product)}\n"
        f"Можно сразу с ценой закупки за 1 {h(product.base_unit)}: <b>10 5000</b>\n"
        "Если пишете в штуках по фасовке, цена за 1 шт: <b>20 шт 3500</b>",
        cancel_kb(f"p:st:d:{dept_id}"),
    )


async def _qty_hint(session, product: Product) -> str:
    units = [u.unit_code for u in dict(await stock.list_products(session)).get(product, [])]
    example = f"Например <b>10</b> (в {h(product.base_unit)})"
    if units:
        example += f" или <b>3 {h(units[0])}</b>"
    return example


# ------------------------------------------------------------------ выдача


@router.callback_query(F.data.startswith("p:st:out:"))
async def issue_start(callback: CallbackQuery, session, actor, notifier):
    dept_id = int(callback.data.split(":")[3])
    actor.require(Perm.STOCK_OPERATE, dept_id)
    buttons = [
        (f"👤 {e.full_name}", f"p:st:oe:{dept_id}:{e.id}") for e in await org.department_employees(session, dept_id)
    ]
    if not buttons:
        raise ValidationError("Сотрудников пока нет")
    show(callback, notifier, "📤 Кому выдать товар?", _grid(buttons, f"p:st:d:{dept_id}"))


@router.callback_query(F.data.startswith("p:st:oe:"))
async def issue_employee(callback: CallbackQuery, session, notifier):
    _, _, _, dept_id, emp_id = callback.data.split(":")
    emp = await session.get(Employee, int(emp_id))
    buttons = await _products_kb(session, int(dept_id), f"p:st:op:{dept_id}:{emp_id}")
    if not buttons:
        raise ValidationError("Товаров пока нет. Сначала добавьте товар")
    show(
        callback,
        notifier,
        f"📤 Что выдать: <b>{h(emp.full_name)}</b>? В скобках остаток на складе.",
        _grid(buttons, f"p:st:d:{dept_id}"),
    )


@router.callback_query(F.data.startswith("p:st:op:"))
async def issue_product(callback: CallbackQuery, state: FSMContext, session, notifier):
    _, _, _, dept_id, emp_id, product_id = callback.data.split(":")
    product = await session.get(Product, int(product_id))
    emp = await session.get(Employee, int(emp_id))
    await ask(
        state,
        "st_qty",
        op="out",
        dept_id=int(dept_id),
        emp_id=emp.id,
        product_id=product.id,
        key=_new_key(),
    )
    show(
        callback,
        notifier,
        f"📤 Сколько <b>{h(product.name)}</b> выдать: {h(emp.full_name)}? {await _qty_hint(session, product)}",
        cancel_kb(f"p:st:d:{dept_id}"),
    )


# ------------------------------------------------------------------ возврат


async def _held(session, dept_id: int, emp_id: int) -> dict[int, Decimal]:
    held: dict[int, Decimal] = {}
    for row in await stock.balances(session, department_id=dept_id, employee_id=emp_id):
        if row.location.kind == "employee":
            held[row.product.id] = held.get(row.product.id, Decimal(0)) + row.qty
    return held


@router.callback_query(F.data.startswith("p:st:ret:"))
async def return_start(callback: CallbackQuery, session, actor, notifier):
    dept_id = int(callback.data.split(":")[3])
    actor.require(Perm.STOCK_OPERATE, dept_id)
    buttons = []
    for emp in await org.department_employees(session, dept_id):
        if await _held(session, dept_id, emp.id):
            buttons.append((f"👤 {emp.full_name}", f"p:st:re:{dept_id}:{emp.id}"))
    if not buttons:
        raise ValidationError("Ни у кого из сотрудников нет товара")
    show(callback, notifier, "↩️ Кто возвращает товар на склад?", _grid(buttons, f"p:st:d:{dept_id}"))


@router.callback_query(F.data.startswith("p:st:re:"))
async def return_employee(callback: CallbackQuery, session, notifier):
    _, _, _, dept_id, emp_id = callback.data.split(":")
    emp = await session.get(Employee, int(emp_id))
    held = await _held(session, int(dept_id), emp.id)
    buttons = await _products_kb(session, int(dept_id), f"p:st:rp:{dept_id}:{emp_id}", only=held)
    show(
        callback,
        notifier,
        f"↩️ Что возвращает <b>{h(emp.full_name)}</b>? В скобках сколько у него.",
        _grid(buttons, f"p:st:d:{dept_id}"),
    )


@router.callback_query(F.data.startswith("p:st:rp:"))
async def return_product(callback: CallbackQuery, state: FSMContext, session, notifier):
    _, _, _, dept_id, emp_id, product_id = callback.data.split(":")
    product = await session.get(Product, int(product_id))
    emp = await session.get(Employee, int(emp_id))
    await ask(
        state,
        "st_qty",
        op="ret",
        dept_id=int(dept_id),
        emp_id=emp.id,
        product_id=product.id,
        key=_new_key(),
    )
    show(
        callback,
        notifier,
        f"↩️ Сколько <b>{h(product.name)}</b> вернул {h(emp.full_name)}? {await _qty_hint(session, product)}",
        cancel_kb(f"p:st:d:{dept_id}"),
    )


# ------------------------------------------------------------------ готовое от сотрудника и израсходовано


@router.callback_query(F.data.startswith("p:st:fin:"))
async def finished_start(callback: CallbackQuery, session, actor, notifier):
    dept_id = int(callback.data.split(":")[3])
    actor.require(Perm.STOCK_OPERATE, dept_id)
    people = await org.department_employees(session, dept_id)
    buttons = [(f"👤 {e.full_name}", f"p:st:fe:{dept_id}:{e.id}") for e in people]
    if not buttons:
        raise ValidationError("Сотрудников пока нет")
    show(
        callback,
        notifier,
        "📦 Кто сдаёт готовый товар на склад? (сфасовал, упаковал, наклеил этикетки)",
        _grid(buttons, f"p:st:d:{dept_id}"),
    )


@router.callback_query(F.data.startswith("p:st:fe:"))
async def finished_employee(callback: CallbackQuery, session, notifier):
    _, _, _, dept_id, emp_id = callback.data.split(":")
    emp = await session.get(Employee, int(emp_id))
    buttons = await _products_kb(session, int(dept_id), f"p:st:fp:{dept_id}:{emp_id}")
    if not buttons:
        raise ValidationError("Товаров пока нет. Сначала добавьте товар: Ещё, Товары")
    markup = _grid(buttons, f"p:st:d:{dept_id}")
    markup.inline_keyboard.insert(0, kb([[("📋 Вставить списком", f"p:st:fl:{dept_id}:{emp_id}")]]).inline_keyboard[0])
    show(
        callback,
        notifier,
        f"📦 Что сдаёт <b>{h(emp.full_name)}</b>?\n"
        "Быстрее всего списком: нажмите 📋 Вставить списком и пришлите, например\n"
        "<code>Крам 05 13\nКрам 1 7\nТрюфель 05 4</code>\n(товар, вес, сколько штук).\n"
        "Или выберите один товар ниже.",
        markup,
    )


@router.callback_query(F.data.startswith("p:st:fl:"))
async def finished_list(callback: CallbackQuery, state: FSMContext, session, notifier):
    _, _, _, dept_id, emp_id = callback.data.split(":")
    emp = await session.get(Employee, int(emp_id))
    await ask(state, "st_list", dept_id=int(dept_id), emp_id=emp.id, key=_new_key())
    show(
        callback,
        notifier,
        f"📋 Пришлите, что принимаете от <b>{h(emp.full_name)}</b>, каждую позицию с новой строки:\n"
        "<code>Крам 05 13\nКрам 1 7\nТрюфель 05 4\nТрюфель 1 17</code>\n"
        "05 и 1 это вес одной штуки, последнее число это сколько штук.\n"
        "Готовый товар (например Крам 0.5) заведётся сам, если есть товар Крам.",
        cancel_kb(f"p:st:d:{dept_id}"),
    )


@form("st_list")
async def form_st_list(message, state, session, actor, notifier, data, **_):
    """Список от оператора: сначала показываем, что поняли, записываем только по кнопке."""
    from ....domain.tags import parse_goods_list

    dept = await session.get(Department, data["dept_id"])
    emp = await session.get(Employee, data["emp_id"])
    actor.require(Perm.STOCK_OPERATE, dept.id)
    lines, other = parse_goods_list(message.text)
    resolved = [(line, await stock.resolve_finished(session, line.name, line.weight)) for line in lines]
    unknown = [line.text for line, g in resolved if g is None] + other
    if unknown or not lines:
        raise ValidationError(
            "Не понял строки: "
            + "; ".join(unknown or [message.text])
            + ". Пишите товар, вес и количество: Крам 05 13. Товар должен быть в списке Товары. "
            "Ничего не записано, пришлите список заново"
        )
    await state.update_data(goods=message.text)
    preview = [f"• {h(g.title)} × {h(line.qty)} шт" for line, g in resolved]
    notifier.answer(
        message,
        f"📦 Принять от <b>{h(emp.full_name)}</b>:\n"
        + "\n".join(preview)
        + "\n\nВсё верно? Пока не нажали, ничего не записано.",
        kb(
            [
                [("✅ Записать на склад", f"p:st:fly:{dept.id}")],
                [("✏️ Прислать заново", f"p:st:fl:{dept.id}:{emp.id}")],
                [("✖️ Отмена", f"p:st:d:{dept.id}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("p:st:fly:"))
async def finished_list_save(callback: CallbackQuery, state: FSMContext, session, actor, settings, notifier):
    from ....domain.tags import parse_goods_list

    data = await state.get_data()
    if data.get("form") != "st_list" or "goods" not in data:
        raise ValidationError("Список уже записан или устарел. Начните заново: Склад, Принять готовое")
    dept = await session.get(Department, data["dept_id"])
    emp = await session.get(Employee, data["emp_id"])
    lines, _ = parse_goods_list(data["goods"])
    done = []
    for index, line in enumerate(lines):
        goods = await stock.resolve_finished(session, line.name, line.weight)
        product = await stock.ensure_finished(session, actor, goods, dept.id)
        await stock.receive(
            session,
            actor,
            key=f"{data['key']}:{index}",
            department=dept,
            product=product,
            qty=parse_decimal(line.qty, what="количество"),
            from_employee=emp,
            comment=f"готовое от {emp.full_name}",
            received_on=settings.today(),
        )
        done.append(f"• {h(goods.title)} × {h(line.qty)} шт")
    await state.clear()
    await session.flush()
    if emp.user_id:
        user = await session.get(User, emp.user_id)
        notifier.send(user.telegram_id, "📦 Склад принял у вас:\n" + "\n".join(done))
    text, markup = await stock_view(session, actor, dept.id)
    show(callback, notifier, f"📦 Принято на склад от {h(emp.full_name)}:\n" + "\n".join(done) + "\n\n" + text, markup)


@router.callback_query(F.data.startswith("p:st:fp:"))
async def finished_product(callback: CallbackQuery, state: FSMContext, session, notifier):
    _, _, _, dept_id, emp_id, product_id = callback.data.split(":")
    product = await session.get(Product, int(product_id))
    emp = await session.get(Employee, int(emp_id))
    await ask(state, "st_qty", op="fin", dept_id=int(dept_id), emp_id=emp.id, product_id=product.id, key=_new_key())
    show(
        callback,
        notifier,
        f"📦 Сколько <b>{h(product.name)}</b> сдал {h(emp.full_name)}? {await _qty_hint(session, product)}",
        cancel_kb(f"p:st:d:{dept_id}"),
    )


@router.callback_query(F.data.startswith("p:st:use:"))
async def used_start(callback: CallbackQuery, session, actor, notifier):
    dept_id = int(callback.data.split(":")[3])
    actor.require(Perm.STOCK_OPERATE, dept_id)
    buttons = []
    for emp in await org.department_employees(session, dept_id):
        if await _held(session, dept_id, emp.id):
            buttons.append((f"👤 {emp.full_name}", f"p:st:ue:{dept_id}:{emp.id}"))
    if not buttons:
        raise ValidationError("Ни у кого из сотрудников нет товара")
    show(
        callback,
        notifier,
        "✂️ У кого списать израсходованное? Например сырьё ушло на фасовку и больше не числится на сотруднике.",
        _grid(buttons, f"p:st:d:{dept_id}"),
    )


@router.callback_query(F.data.startswith("p:st:ue:"))
async def used_employee(callback: CallbackQuery, session, notifier):
    _, _, _, dept_id, emp_id = callback.data.split(":")
    emp = await session.get(Employee, int(emp_id))
    held = await _held(session, int(dept_id), emp.id)
    buttons = await _products_kb(session, int(dept_id), f"p:st:up:{dept_id}:{emp_id}", only=held)
    show(
        callback,
        notifier,
        f"✂️ Что израсходовал <b>{h(emp.full_name)}</b>? В скобках сколько у него.",
        _grid(buttons, f"p:st:d:{dept_id}"),
    )


@router.callback_query(F.data.startswith("p:st:up:"))
async def used_product(callback: CallbackQuery, state: FSMContext, session, notifier):
    _, _, _, dept_id, emp_id, product_id = callback.data.split(":")
    product = await session.get(Product, int(product_id))
    emp = await session.get(Employee, int(emp_id))
    await ask(state, "st_qty", op="use", dept_id=int(dept_id), emp_id=emp.id, product_id=product.id, key=_new_key())
    show(
        callback,
        notifier,
        f"✂️ Сколько <b>{h(product.name)}</b> израсходовал {h(emp.full_name)}? {await _qty_hint(session, product)}",
        cancel_kb(f"p:st:d:{dept_id}"),
    )


def _new_key() -> str:
    return f"st:{uuid.uuid4().hex}"


STOCK_HELP = """❓ <b>Как работать со складом</b>

<b>1. Товар пришёл на склад</b>
📥 Приход, выберите товар, напишите сколько. Например пришло 20 пачек по 250 г:
если товар считается в гр, а фасовка 1 шт = 250 гр, напишите <code>20 шт</code>.

<b>2. Дать товар сотруднику</b> (фасовать, клеить этикетки, развозить)
📤 Выдать, сотрудник, товар, количество. Сотруднику придёт кнопка Получено.
Пока он не нажал, товар висит как 🚚 ждёт подтверждения.

<b>3. Сотрудник сделал работу</b> (фасовка, факт, доставка)
Он пишет в своей группе, например <code>#фасовка крам 1 40</code> или <code>Крам факт 0.5 13</code>.
Оператор подтверждает кнопкой, и деньги идут сотруднику в зарплату.

<b>4. Принять готовое на склад</b> (это делает оператор, не сотрудник)
В группе сотрудника напишите:
<code>#принял
Крам 05 13
Трюфель 1 17</code>
Бот покажет, что понял. Проверьте и нажмите ✅ Записать на склад. До кнопки ничего не записано.
Ошиблись после записи: ↩️ Отменить приём, товар снимется со склада.
Или здесь: 📦 Принять готовое, сотрудник, 📋 Вставить списком.
Готовый товар (Крам 0.5, Крам 1) заводится сам, если есть товар Крам.
Сырьё, которое ушло на фасовку: ✂️ Израсходовано, тогда оно перестанет числиться на сотруднике.

<b>5. Сотрудник вернул лишнее</b>
↩️ Возврат: товар снимается с сотрудника и возвращается на склад.

📜 История покажет, кто, что и когда получил или сдал."""


@router.callback_query(F.data.startswith("p:st:help:"))
async def stock_help(callback: CallbackQuery, notifier):
    dept_id = int(callback.data.split(":")[3])
    show(callback, notifier, STOCK_HELP, kb([[("⬅️ Склад", f"p:st:d:{dept_id}")]]))


# ------------------------------------------------------------------ количество текстом


@form("st_qty")
async def form_st_qty(message, state, session, actor, settings, notifier, data, **_):
    from ..stock import _ask_recipient

    items = message.text.replace(",", ".").split()
    if not items:
        raise ValidationError("Напишите количество, например 10")
    qty = parse_decimal(items[0], what="количество")
    rest = items[1:]
    unit = None
    if rest and rest[0] in {u.code for u in await reference.list_units(session)}:
        unit = rest.pop(0)
    dept = await session.get(Department, data["dept_id"])
    product = await session.get(Product, data["product_id"])
    emp = await session.get(Employee, data["emp_id"]) if data.get("emp_id") else None
    common = {"key": data["key"], "department": dept, "product": product, "qty": qty, "unit": unit}
    if data["op"] == "in":
        cost = parse_decimal(rest[0], allow_zero=True, what="цену") if rest else None
        result = await stock.receive(
            session,
            actor,
            **common,
            unit_cost=cost,
            currency=settings.currency if cost is not None else None,
            received_on=settings.today(),
        )
        done = "📥 Приход записан"
    elif data["op"] == "out":
        result = await stock.issue(session, actor, employee=emp, **common)
        done = "📤 Выдано. Сотрудник должен нажать Получено, тогда товар запишется на него"
    elif data["op"] == "fin":
        result = await stock.receive(
            session,
            actor,
            **common,
            from_employee=emp,
            comment=f"готовое от {emp.full_name}",
            received_on=settings.today(),
        )
        done = f"📦 Принято на склад от {h(emp.full_name)}"
    elif data["op"] == "use":
        result = await stock.write_off(session, actor, employee=emp, reason="израсходовано на работу", **common)
        done = f"✂️ Списано у {h(emp.full_name)} как израсходованное"
    else:
        result = await stock.return_to_stock(session, actor, employee=emp, **common)
        done = "↩️ Возврат записан"
    await state.clear()
    card = await ui.stock_doc_card(session, result.document)
    if data["op"] == "out" and result.created:
        await _ask_recipient(session, notifier, result.document, card)
    elif data["op"] in ("fin", "use", "ret") and result.created and emp.user_id:
        # Сотруднику видно, что склад записал: что принято от него или списано с него.
        user = await session.get(User, emp.user_id)
        notifier.send(user.telegram_id, "📦 Склад записал:\n" + card)
    await session.flush()
    text, markup = await stock_view(session, actor, dept.id)
    notifier.answer(message, f"{done}\n{card}")
    notifier.answer(message, text, markup)


# ------------------------------------------------------------------ история


@router.callback_query(F.data.startswith("p:st:log:"))
async def stock_log(callback: CallbackQuery, session, actor, settings, notifier):
    _can_view(actor)
    dept_id = int(callback.data.split(":")[3])
    docs = (
        (
            await session.execute(
                select(StockDocument)
                .where(StockDocument.department_id == dept_id)
                .order_by(StockDocument.id.desc())
                .limit(15)
            )
        )
        .scalars()
        .all()
    )
    lines = ["📜 <b>История склада</b> (последние 15)", ""]
    for doc in docs:
        when = doc.created_at.astimezone(settings.tz)
        what = []
        for line, product in await stock.document_lines(session, doc.id):
            what.append(f"{h(product.name)} {fmt_qty(line.qty_entered, line.unit_entered)}")
        who = ""
        emp_id = doc.target_employee_id or doc.employee_id
        if emp_id:
            who = f" · {h((await session.get(Employee, emp_id)).full_name)}"
        mark = {"in_transit": " 🚚", "returned": " ✖️"}.get(doc.status, "")
        lines.append(f"{when:%d.%m %H:%M} {stock.DOC_TITLES[doc.doc_type]}{who}: {', '.join(what)}{mark}")
    if len(lines) == 2:
        lines.append("Пока пусто.")
    else:
        lines += ["", "🚚 ждёт подтверждения сотрудника, ✖️ сотрудник не принял"]
    show(callback, notifier, "\n".join(lines), kb([[("⬅️ Склад", f"p:st:d:{dept_id}")]]))
