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

from ....db.models import Department, Employee, Product, StockDocument, StockDocumentLine
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
    rows = await session.execute(
        select(StockDocumentLine.product_id, StockDocument.doc_type, func.sum(StockDocumentLine.qty_base))
        .join(StockDocument, StockDocument.id == StockDocumentLine.document_id)
        .where(
            StockDocument.department_id == dept_id,
            StockDocument.status != "returned",
            StockDocument.doc_type.in_(("receipt", "issue", "return")),
        )
        .group_by(StockDocumentLine.product_id, StockDocument.doc_type)
    )
    result: dict[int, dict[str, Decimal]] = {}
    for product_id, doc_type, qty in rows.all():
        result.setdefault(product_id, {})[doc_type] = qty
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
        lines.append(
            f"пришло {fmt_number(t.get('receipt', Decimal(0)))} · выдано {fmt_number(t.get('issue', Decimal(0)))}"
            f" · вернули {fmt_number(t.get('return', Decimal(0)))} {h(u)}"
        )
        lines.append(f"🏬 на складе: <b>{fmt_qty(p['warehouse'] if p else Decimal(0), u)}</b>")
        for emp, qty in (p or {}).get("people", []):
            lines.append(f"👤 {h(emp.full_name)}: {fmt_qty(qty, u)}")
        if p and p["transit"]:
            lines.append(f"🚚 выдано, ждёт подтверждения: {fmt_qty(p['transit'], u)}")
        lines.append("")
    if len(lines) == 2:
        lines.append("Движений пока нет. Начните с прихода товара на склад.")
    rows = [
        [("📥 Приход", f"p:st:in:{dept_id}"), ("📤 Выдать", f"p:st:out:{dept_id}")],
        [("↩️ Возврат", f"p:st:ret:{dept_id}"), ("📜 История", f"p:st:log:{dept_id}")],
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
    await ask(state, "st_qty", op="in", dept_id=int(dept_id), product_id=product.id, key=f"st:{uuid.uuid4().hex}")
    show(
        callback,
        notifier,
        f"📥 Сколько пришло <b>{h(product.name)}</b>? {await _qty_hint(session, product)}\n"
        f"Можно сразу с ценой закупки за 1 {h(product.base_unit)}: <b>10 5000</b>",
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
        key=f"st:{uuid.uuid4().hex}",
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
        key=f"st:{uuid.uuid4().hex}",
    )
    show(
        callback,
        notifier,
        f"↩️ Сколько <b>{h(product.name)}</b> вернул {h(emp.full_name)}? {await _qty_hint(session, product)}",
        cancel_kb(f"p:st:d:{dept_id}"),
    )


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
    else:
        result = await stock.return_to_stock(session, actor, employee=emp, **common)
        done = "↩️ Возврат записан"
    await state.clear()
    card = await ui.stock_doc_card(session, result.document)
    if data["op"] == "out" and result.created:
        await _ask_recipient(session, notifier, result.document, card)
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
