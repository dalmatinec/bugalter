"""Складской учёт.

Каждое движение — строка stock_moves «откуда → куда». Поставщик, клиенты, списание, недостача и излишек —
условные места (могут уходить в минус), склад/сотрудник/«в пути» — реальные (остаток не меньше нуля).
Выдача сотруднику и перемещения идут через «в пути» и завершаются подтверждением получения.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    VIRTUAL_LOCATION_KINDS,
    Department,
    DiscrepancyCase,
    Employee,
    Location,
    Product,
    ProductUnit,
    StockBalance,
    StockDocument,
    StockDocumentLine,
    StockMove,
    Unit,
)
from ..domain.errors import AccessDenied, Conflict, InsufficientStock, NotFound, ValidationError
from ..domain.money import currency_step, fmt_qty, round_to_step, scale_of
from ..domain.permissions import Perm
from . import org, payroll, reference
from .audit import audit
from .settings import Settings

LOCATION_TITLES = {
    "warehouse": "склад",
    "employee": "на руках",
    "transit": "в пути",
    "supplier": "поставщик",
    "customer": "клиенты",
    "writeoff": "списание",
    "shortage": "недостача",
    "surplus": "излишек",
}
DOC_TITLES = {
    "receipt": "Поступление",
    "issue": "Выдача сотруднику",
    "return": "Возврат на склад",
    "move": "Передача между сотрудниками",
    "send": "Перемещение в подразделение",
    "delivery": "Отгрузка клиенту",
    "writeoff": "Списание",
    "inventory": "Инвентаризация",
}


# ------------------------------------------------------------------ номенклатура


async def create_product(
    session: AsyncSession, actor, sku: str, unit_code: str, name: str, category: str | None = None
) -> Product:
    actor.require(Perm.STOCK_MANAGE)
    sku = sku.strip().lower()
    if not sku or " " in sku or len(sku) > 64:
        raise ValidationError("Артикул — одно слово до 64 символов")
    await reference.get_unit(session, unit_code)
    if await get_product(session, sku, required=False):
        raise ValidationError(f"Товар {sku} уже есть")
    product = Product(sku=sku, name=name.strip() or sku, base_unit=unit_code, category=category)
    session.add(product)
    await session.flush()
    await audit(session, actor.user.id, "stock.product_create", "product", product.id, {"sku": sku, "unit": unit_code})
    return product


async def add_product_unit(session: AsyncSession, actor, product: Product, unit_code: str, factor: Decimal) -> None:
    actor.require(Perm.STOCK_MANAGE)
    await reference.get_unit(session, unit_code)
    if unit_code == product.base_unit:
        raise ValidationError("Это базовая единица товара")
    if factor <= 0:
        raise ValidationError("Коэффициент должен быть больше нуля")
    if await session.get(ProductUnit, (product.id, unit_code)):
        raise ValidationError("Коэффициент уже задан. Чтобы не исказить историю, он не изменяется")
    base = await session.get(Unit, product.base_unit)
    if scale_of(factor) > base.scale:
        raise ValidationError(
            f"1 {unit_code} = {factor} {base.code}: базовая единица допускает {base.scale} знаков после запятой"
        )
    session.add(ProductUnit(product_id=product.id, unit_code=unit_code, factor=factor))
    await audit(
        session, actor.user.id, "stock.product_unit", "product", product.id, {"unit": unit_code, "factor": factor}
    )


async def get_product(session: AsyncSession, sku: str, required: bool = True) -> Product | None:
    product = (await session.execute(select(Product).where(Product.sku == sku.strip().lower()))).scalar_one_or_none()
    if product is None and required:
        raise NotFound(f"Нет товара «{sku}». Список: /products")
    return product


async def list_products(session: AsyncSession) -> list[tuple[Product, list[ProductUnit]]]:
    products = list((await session.execute(select(Product).order_by(Product.sku))).scalars())
    units = list((await session.execute(select(ProductUnit))).scalars())
    by_product: dict[int, list[ProductUnit]] = {}
    for pu in units:
        by_product.setdefault(pu.product_id, []).append(pu)
    return [(p, by_product.get(p.id, [])) for p in products]


async def to_base(session: AsyncSession, product: Product, qty: Decimal, unit_code: str | None) -> tuple[Decimal, str]:
    """Пересчёт в базовую единицу с проверкой точности: 1,5 шт — ошибка, 3 пачки по 0,25 кг — 0,75 кг."""
    if qty <= 0:
        raise ValidationError("Количество должно быть больше нуля")
    unit_code = unit_code or product.base_unit
    base = await session.get(Unit, product.base_unit)
    if unit_code == product.base_unit:
        factor = Decimal(1)
        entered_unit = base
    else:
        pu = await session.get(ProductUnit, (product.id, unit_code))
        if pu is None:
            raise ValidationError(
                f"Для «{product.name}» нет единицы «{unit_code}». Базовая — {product.base_unit}; "
                f"добавить: /product_unit {product.sku} {unit_code} <коэффициент>"
            )
        factor = pu.factor
        entered_unit = await session.get(Unit, unit_code)
    if scale_of(qty) > entered_unit.scale:
        raise ValidationError(f"Для «{entered_unit.code}» допускается {entered_unit.scale} знаков после запятой")
    qty_base = qty * factor
    if scale_of(qty_base) > base.scale:
        raise ValidationError(
            f"{fmt_qty(qty, unit_code)} = {fmt_qty(qty_base, base.code)}: "
            f"для «{base.code}» допускается {base.scale} знаков после запятой"
        )
    return qty_base.normalize(), unit_code


# ------------------------------------------------------------------ места хранения и остатки


async def location(session: AsyncSession, kind: str, department_id: int, employee_id: int | None = None) -> Location:
    stmt = (
        insert(Location)
        .values(kind=kind, department_id=department_id, employee_id=employee_id)
        .on_conflict_do_nothing()
    )
    await session.execute(stmt)
    query = select(Location).where(Location.kind == kind, Location.department_id == department_id)
    query = query.where(Location.employee_id.is_(None) if employee_id is None else Location.employee_id == employee_id)
    return (await session.execute(query)).scalar_one()


async def balance(session: AsyncSession, location_id: int, product_id: int, lock: bool = False) -> Decimal:
    query = select(StockBalance.qty).where(
        StockBalance.location_id == location_id, StockBalance.product_id == product_id, StockBalance.batch_id.is_(None)
    )
    if lock:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none() or Decimal(0)


async def _apply(session: AsyncSession, loc: Location, product_id: int, delta: Decimal) -> None:
    virtual = loc.kind in VIRTUAL_LOCATION_KINDS
    if delta < 0 and not virtual:
        # Списание с реального места — только UPDATE существующей строки: при INSERT … ON CONFLICT
        # PostgreSQL проверяет CHECK (qty >= 0) на предлагаемой отрицательной строке ещё до конфликта.
        result = await session.execute(
            update(StockBalance)
            .where(
                StockBalance.location_id == loc.id,
                StockBalance.product_id == product_id,
                StockBalance.batch_id.is_(None),
            )
            .values(qty=StockBalance.qty + delta)
        )
        if result.rowcount != 1:
            raise InsufficientStock("Нет остатка для списания")
        return
    stmt = insert(StockBalance).values(
        location_id=loc.id,
        product_id=product_id,
        batch_id=None,
        qty=delta,
        allow_negative=virtual,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["location_id", "product_id"],
        index_where=text("batch_id IS NULL"),
        set_={"qty": StockBalance.qty + delta},
    )
    await session.execute(stmt)


async def _move(
    session: AsyncSession,
    doc: StockDocument,
    product: Product,
    src: Location,
    dst: Location,
    qty: Decimal,
    user_id: int,
) -> None:
    if src.kind not in VIRTUAL_LOCATION_KINDS:
        # Блокируем строки остатков в одном порядке (по id места), чтобы параллельные движения не взаимоблокировались.
        for loc in sorted((src, dst), key=lambda x: x.id):
            if loc.kind not in VIRTUAL_LOCATION_KINDS:
                await balance(session, loc.id, product.id, lock=True)
        have = await balance(session, src.id, product.id)
        if have < qty:
            raise InsufficientStock(
                f"Недостаточно «{product.name}» ({await describe_location(session, src)}): "
                f"есть {fmt_qty(have, product.base_unit)}, нужно {fmt_qty(qty, product.base_unit)}"
            )
    session.add(
        StockMove(
            document_id=doc.id,
            product_id=product.id,
            from_location_id=src.id,
            to_location_id=dst.id,
            qty=qty,
            posted_by=user_id,
        )
    )
    await _apply(session, src, product.id, -qty)
    await _apply(session, dst, product.id, qty)


async def describe_location(session: AsyncSession, loc: Location) -> str:
    dept = await session.get(Department, loc.department_id)
    if loc.kind == "employee" and loc.employee_id:
        emp = await session.get(Employee, loc.employee_id)
        return f"{emp.full_name} ({dept.code})"
    return f"{LOCATION_TITLES[loc.kind]} {dept.code}"


# ------------------------------------------------------------------ документы


@dataclass
class DocResult:
    document: StockDocument
    created: bool
    qty_base: Decimal | None = None


async def _existing_doc(session: AsyncSession, key: str) -> StockDocument | None:
    return (
        await session.execute(select(StockDocument).where(StockDocument.idempotency_key == key))
    ).scalar_one_or_none()


async def _new_doc(
    session: AsyncSession,
    actor,
    *,
    key: str,
    doc_type: str,
    department_id: int,
    status: str,
    chat_id: int | None,
    **extra,
) -> StockDocument | None:
    stmt = (
        insert(StockDocument)
        .values(
            idempotency_key=key,
            doc_type=doc_type,
            department_id=department_id,
            status=status,
            created_by=actor.user.id,
            chat_id=chat_id,
            **extra,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(StockDocument.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    return await session.get(StockDocument, new_id) if new_id else None


async def _simple_doc(
    session: AsyncSession,
    actor,
    *,
    key: str,
    doc_type: str,
    department: Department,
    product: Product,
    qty: Decimal,
    unit: str | None,
    src: Location,
    dst: Location,
    final: Location | None = None,
    perm: str = Perm.STOCK_OPERATE,
    chat_id: int | None = None,
    **extra,
) -> DocResult:
    existing = await _existing_doc(session, key)
    if existing:
        return DocResult(existing, False)
    actor.require(perm, department.id)
    qty_base, unit_code = await to_base(session, product, qty, unit)
    status = "in_transit" if final is not None else "posted"
    doc = await _new_doc(
        session, actor, key=key, doc_type=doc_type, department_id=department.id, status=status, chat_id=chat_id, **extra
    )
    if doc is None:
        return DocResult(await _existing_doc(session, key), False)
    session.add(
        StockDocumentLine(
            document_id=doc.id,
            product_id=product.id,
            qty_entered=qty,
            unit_entered=unit_code,
            qty_base=qty_base,
            from_location_id=src.id,
            to_location_id=dst.id,
            final_location_id=final.id if final else None,
        )
    )
    await _move(session, doc, product, src, dst, qty_base, actor.user.id)
    await audit(
        session,
        actor.user.id,
        f"stock.{doc_type}",
        "stock_document",
        doc.id,
        {
            "product": product.sku,
            "qty": qty,
            "unit": unit_code,
            "qty_base": qty_base,
            "from": src.id,
            "to": dst.id,
            **{k: v for k, v in extra.items() if v is not None},
        },
        chat_id,
    )
    return DocResult(doc, True, qty_base)


async def _member(session: AsyncSession, employee: Employee, department: Department) -> None:
    if not await org.is_member(session, employee.id, department.id):
        raise ValidationError(f"{employee.full_name} не состоит в подразделении {department.code}")


async def receive(session, actor, *, key, department, product, qty, unit=None, comment=None, chat_id=None) -> DocResult:
    return await _simple_doc(
        session,
        actor,
        key=key,
        doc_type="receipt",
        department=department,
        product=product,
        qty=qty,
        unit=unit,
        src=await location(session, "supplier", department.id),
        dst=await location(session, "warehouse", department.id),
        comment=comment,
        chat_id=chat_id,
    )


async def issue(session, actor, *, key, department, employee, product, qty, unit=None, comment=None, chat_id=None):
    """Выдача сотруднику: склад → в пути; на сотрудника товар ляжет после подтверждения получения."""
    await _member(session, employee, department)
    return await _simple_doc(
        session,
        actor,
        key=key,
        doc_type="issue",
        department=department,
        product=product,
        qty=qty,
        unit=unit,
        src=await location(session, "warehouse", department.id),
        dst=await location(session, "transit", department.id),
        final=await location(session, "employee", department.id, employee.id),
        target_employee_id=employee.id,
        comment=comment,
        chat_id=chat_id,
    )


async def transfer(
    session, actor, *, key, department, from_employee, to_employee, product, qty, unit=None, comment=None, chat_id=None
):
    if from_employee.id == to_employee.id:
        raise ValidationError("Отправитель и получатель совпадают")
    await _member(session, from_employee, department)
    await _member(session, to_employee, department)
    return await _simple_doc(
        session,
        actor,
        key=key,
        doc_type="move",
        department=department,
        product=product,
        qty=qty,
        unit=unit,
        src=await location(session, "employee", department.id, from_employee.id),
        dst=await location(session, "transit", department.id),
        final=await location(session, "employee", department.id, to_employee.id),
        employee_id=from_employee.id,
        target_employee_id=to_employee.id,
        comment=comment,
        chat_id=chat_id,
    )


async def send(session, actor, *, key, department, target, product, qty, unit=None, comment=None, chat_id=None):
    """Перемещение со склада подразделения на склад другого. Получение подтверждает принимающая сторона."""
    if target.id == department.id:
        raise ValidationError("Подразделение-получатель совпадает с отправителем")
    return await _simple_doc(
        session,
        actor,
        key=key,
        doc_type="send",
        department=department,
        product=product,
        qty=qty,
        unit=unit,
        src=await location(session, "warehouse", department.id),
        dst=await location(session, "transit", target.id),
        final=await location(session, "warehouse", target.id),
        target_department_id=target.id,
        comment=comment,
        chat_id=chat_id,
    )


async def return_to_stock(
    session, actor, *, key, department, employee, product, qty, unit=None, comment=None, chat_id=None
):
    return await _simple_doc(
        session,
        actor,
        key=key,
        doc_type="return",
        department=department,
        product=product,
        qty=qty,
        unit=unit,
        src=await location(session, "employee", department.id, employee.id),
        dst=await location(session, "warehouse", department.id),
        employee_id=employee.id,
        comment=comment,
        chat_id=chat_id,
    )


async def deliver(
    session, actor, *, key, department, employee, product, qty, unit=None, order_ref=None, comment=None, chat_id=None
):
    return await _simple_doc(
        session,
        actor,
        key=key,
        doc_type="delivery",
        department=department,
        product=product,
        qty=qty,
        unit=unit,
        src=await location(session, "employee", department.id, employee.id),
        dst=await location(session, "customer", department.id),
        employee_id=employee.id,
        order_ref=order_ref,
        comment=comment,
        chat_id=chat_id,
    )


async def write_off(session, actor, *, key, department, product, qty, unit=None, reason, employee=None, chat_id=None):
    if not reason or not reason.strip():
        raise ValidationError("Для списания нужна причина")
    src = (
        await location(session, "employee", department.id, employee.id)
        if employee
        else await location(session, "warehouse", department.id)
    )
    return await _simple_doc(
        session,
        actor,
        key=key,
        doc_type="writeoff",
        department=department,
        product=product,
        qty=qty,
        unit=unit,
        src=src,
        dst=await location(session, "writeoff", department.id),
        employee_id=employee.id if employee else None,
        comment=reason,
        chat_id=chat_id,
    )


def _can_accept(actor, doc: StockDocument) -> bool:
    if doc.target_employee_id is not None and actor.is_self(doc.target_employee_id):
        return True
    dept_id = doc.target_department_id or doc.department_id
    return actor.can(Perm.STOCK_RECEIPT_CONFIRM, dept_id)


async def accept(session: AsyncSession, actor, document_id: int) -> tuple[StockDocument, bool]:
    """Подтверждение получения: в пути → получатель. Подтверждает сам получатель или оператор с правом."""
    doc = await session.get(StockDocument, document_id, with_for_update=True, populate_existing=True)
    if doc is None:
        raise NotFound(f"Документ #{document_id} не найден")
    if doc.status == "completed":
        return doc, False
    if doc.status != "in_transit":
        raise Conflict(f"{doc.number}: товар не в пути")
    if not _can_accept(actor, doc):
        raise AccessDenied("Подтвердить получение может получатель или оператор с правом подтверждения")
    lines = (await session.execute(select(StockDocumentLine).where(StockDocumentLine.document_id == doc.id))).scalars()
    for line in lines:
        product = await session.get(Product, line.product_id)
        await _move(
            session,
            doc,
            product,
            await session.get(Location, line.to_location_id),
            await session.get(Location, line.final_location_id),
            line.qty_base,
            actor.user.id,
        )
    doc.status = "completed"
    doc.accepted_by = actor.user.id
    doc.accepted_at = func.now()
    await audit(session, actor.user.id, "stock.accept", "stock_document", doc.id, None, doc.chat_id)
    return doc, True


async def decline(session: AsyncSession, actor, document_id: int, reason: str | None = None):
    """Получатель не принял товар: возвращаем из «в пути» туда, откуда взяли."""
    doc = await session.get(StockDocument, document_id, with_for_update=True, populate_existing=True)
    if doc is None:
        raise NotFound(f"Документ #{document_id} не найден")
    if doc.status == "returned":
        return doc, False
    if doc.status != "in_transit":
        raise Conflict(f"{doc.number}: товар не в пути")
    if not (_can_accept(actor, doc) or actor.can(Perm.STOCK_OPERATE, doc.department_id)):
        raise AccessDenied("Недостаточно прав")
    lines = (await session.execute(select(StockDocumentLine).where(StockDocumentLine.document_id == doc.id))).scalars()
    for line in lines:
        product = await session.get(Product, line.product_id)
        await _move(
            session,
            doc,
            product,
            await session.get(Location, line.to_location_id),
            await session.get(Location, line.from_location_id),
            line.qty_base,
            actor.user.id,
        )
    doc.status = "returned"
    doc.accepted_by = actor.user.id
    doc.accepted_at = func.now()
    await audit(session, actor.user.id, "stock.decline", "stock_document", doc.id, {"reason": reason}, doc.chat_id)
    return doc, True


# ------------------------------------------------------------------ инвентаризация и расхождения


@dataclass
class InventoryResult:
    document: StockDocument
    created: bool
    expected: Decimal
    counted: Decimal
    case: DiscrepancyCase | None


async def inventory(
    session: AsyncSession,
    actor,
    *,
    key: str,
    department: Department,
    product: Product,
    counted: Decimal,
    unit: str | None = None,
    employee: Employee | None = None,
    comment: str | None = None,
    chat_id: int | None = None,
) -> InventoryResult:
    existing = await _existing_doc(session, key)
    if existing:
        case = (
            await session.execute(select(DiscrepancyCase).where(DiscrepancyCase.document_id == existing.id))
        ).scalar_one_or_none()
        return InventoryResult(existing, False, Decimal(0), Decimal(0), case)
    actor.require(Perm.STOCK_INVENTORY, department.id)
    if employee:
        await _member(session, employee, department)
    loc = (
        await location(session, "employee", department.id, employee.id)
        if employee
        else await location(session, "warehouse", department.id)
    )
    if counted < 0:
        raise ValidationError("Фактическое количество не может быть отрицательным")
    counted_base = Decimal(0) if counted == 0 else (await to_base(session, product, counted, unit))[0]
    expected = await balance(session, loc.id, product.id, lock=True)
    doc = await _new_doc(
        session,
        actor,
        key=key,
        doc_type="inventory",
        department_id=department.id,
        status="posted",
        chat_id=chat_id,
        employee_id=employee.id if employee else None,
        comment=comment,
    )
    if doc is None:
        return await inventory(
            session,
            actor,
            key=key,
            department=department,
            product=product,
            counted=counted,
            unit=unit,
            employee=employee,
            comment=comment,
            chat_id=chat_id,
        )
    diff = counted_base - expected
    case = None
    if diff != 0:
        kind = "shortage" if diff < 0 else "surplus"
        virtual = await location(session, kind, department.id)
        src, dst = (loc, virtual) if diff < 0 else (virtual, loc)
        session.add(
            StockDocumentLine(
                document_id=doc.id,
                product_id=product.id,
                qty_entered=abs(diff),
                unit_entered=product.base_unit,
                qty_base=abs(diff),
                from_location_id=src.id,
                to_location_id=dst.id,
            )
        )
        await _move(session, doc, product, src, dst, abs(diff), actor.user.id)
        case = DiscrepancyCase(
            document_id=doc.id,
            department_id=department.id,
            product_id=product.id,
            location_id=loc.id,
            employee_id=employee.id if employee else None,
            kind=kind,
            qty=abs(diff),
            expected_qty=expected,
            counted_qty=counted_base,
            status="open",
        )
        session.add(case)
        await session.flush()
    await audit(
        session,
        actor.user.id,
        "stock.inventory",
        "stock_document",
        doc.id,
        {
            "product": product.sku,
            "location_id": loc.id,
            "expected": expected,
            "counted": counted_base,
            "case_id": case.id if case else None,
        },
        chat_id,
    )
    return InventoryResult(doc, True, expected, counted_base, case)


async def decide_case(
    session: AsyncSession,
    actor,
    settings: Settings,
    case_id: int,
    *,
    decision: str,
    price: Decimal | None = None,
    currency: str | None = None,
    price_basis: str | None = None,
    comment: str | None = None,
):
    """Решение по расхождению. «employee» создаёт документ удержания, который ещё нужно подписать."""
    case = await session.get(DiscrepancyCase, case_id, with_for_update=True, populate_existing=True)
    if case is None:
        raise NotFound(f"Расхождение #{case_id} не найдено")
    if case.status == "resolved":
        raise Conflict(f"{case.number} уже рассмотрено")
    actor.require(Perm.STOCK_CASE_DECIDE, case.department_id)
    if decision not in ("company", "employee"):
        raise ValidationError("Решение: company (за счёт компании) или employee (на сотрудника)")
    adjustment = None
    if decision == "employee":
        if case.kind != "shortage":
            raise ValidationError("На сотрудника относится только недостача")
        if case.employee_id is None:
            raise ValidationError("Недостача на складе не закреплена за сотрудником — решение только «company»")
        if actor.is_self(case.employee_id) and not actor.is_owner:
            raise AccessDenied("Нельзя решать расхождение по себе")
        if price is None or price <= 0:
            raise ValidationError("Укажите цену за базовую единицу товара")
        if not price_basis or not price_basis.strip():
            raise ValidationError("Укажите основание цены (закупочная, продажная, по договорённости…)")
        currency_code = (currency or settings.currency).upper()
        cur = await reference.get_currency(session, currency_code)
        product = await session.get(Product, case.product_id)
        amount = round_to_step(case.qty * price, currency_step(cur.minor_units), settings.rounding_mode)
        employee = await session.get(Employee, case.employee_id)
        adjustment, _ = await payroll.create_adjustment(
            session,
            actor,
            settings,
            kind="deduction",
            employee=employee,
            amount=amount,
            currency=currency_code,
            reason=f"Недостача {case.number}: {product.name} {fmt_qty(case.qty, product.base_unit)} × {price} "
            f"({price_basis.strip()})",
            idempotency_key=f"case:{case.id}",
            department_id=case.department_id,
            basis_type="discrepancy",
            basis_id=case.id,
            check_permission=False,
        )
        case.price, case.currency, case.amount, case.price_basis = price, currency_code, amount, price_basis.strip()
        case.adjustment_id = adjustment.id
    case.status = "resolved"
    case.decision = decision
    case.comment = comment
    case.decided_by = actor.user.id
    case.decided_at = func.now()
    await audit(
        session,
        actor.user.id,
        "stock.case_decide",
        "discrepancy_case",
        case.id,
        {"decision": decision, "price": price, "amount": case.amount, "adjustment_id": case.adjustment_id},
    )
    return case, adjustment


async def open_cases(session: AsyncSession, department_id: int | None = None) -> list[DiscrepancyCase]:
    query = select(DiscrepancyCase).where(DiscrepancyCase.status == "open").order_by(DiscrepancyCase.id)
    if department_id is not None:
        query = query.where(DiscrepancyCase.department_id == department_id)
    return list((await session.execute(query)).scalars())


async def get_document(session: AsyncSession, document_id: int) -> StockDocument:
    doc = await session.get(StockDocument, document_id)
    if doc is None:
        raise NotFound(f"Документ #{document_id} не найден")
    return doc


async def document_lines(session: AsyncSession, document_id: int) -> list[tuple[StockDocumentLine, Product]]:
    rows = (
        await session.execute(
            select(StockDocumentLine, Product)
            .join(Product, Product.id == StockDocumentLine.product_id)
            .where(StockDocumentLine.document_id == document_id)
        )
    ).all()
    return [tuple(r) for r in rows]


@dataclass
class BalanceRow:
    location: Location
    location_title: str
    product: Product
    qty: Decimal


async def balances(
    session: AsyncSession,
    *,
    department_id: int | None = None,
    employee_id: int | None = None,
    include_virtual: bool = False,
) -> list[BalanceRow]:
    query = (
        select(StockBalance, Location, Product)
        .join(Location, Location.id == StockBalance.location_id)
        .join(Product, Product.id == StockBalance.product_id)
        .where(StockBalance.qty != 0)
        .order_by(Location.department_id, Location.kind, Location.employee_id, Product.sku)
    )
    if department_id is not None:
        query = query.where(Location.department_id == department_id)
    if employee_id is not None:
        query = query.where(Location.employee_id == employee_id)
    if not include_virtual:
        query = query.where(Location.kind.notin_(tuple(VIRTUAL_LOCATION_KINDS)))
    result = []
    for bal, loc, product in (await session.execute(query)).all():
        result.append(BalanceRow(loc, await describe_location(session, loc), product, bal.qty))
    return result


async def moves(
    session: AsyncSession, *, department_id: int, date_from: dt.date, date_to: dt.date, tz: str
) -> list[tuple]:
    local_date = func.date(func.timezone(tz, StockMove.posted_at))
    query = (
        select(StockMove, StockDocument, Product)
        .join(StockDocument, StockDocument.id == StockMove.document_id)
        .join(Product, Product.id == StockMove.product_id)
        .where(
            (StockDocument.department_id == department_id) | (StockDocument.target_department_id == department_id),
            local_date.between(date_from, date_to),
        )
        .order_by(StockMove.id)
    )
    return [tuple(r) for r in (await session.execute(query)).all()]


async def pending_transfers(session: AsyncSession, actor) -> list[StockDocument]:
    docs = (
        await session.execute(
            select(StockDocument).where(StockDocument.status == "in_transit").order_by(StockDocument.id)
        )
    ).scalars()
    return [d for d in docs if _can_accept(actor, d)]


async def verify_balances(session: AsyncSession) -> list[tuple[int, int, Decimal, Decimal]]:
    """Сверка остатков с журналом движений. Пустой список — расхождений нет."""
    rows = (
        await session.execute(
            text(
                """
                WITH flows AS (
                    SELECT to_location_id AS location_id, product_id, qty FROM stock_moves
                    UNION ALL
                    SELECT from_location_id, product_id, -qty FROM stock_moves
                ), totals AS (
                    SELECT location_id, product_id, SUM(qty) AS qty FROM flows GROUP BY 1, 2
                )
                SELECT COALESCE(t.location_id, b.location_id), COALESCE(t.product_id, b.product_id),
                       COALESCE(t.qty, 0), COALESCE(b.qty, 0)
                FROM totals t
                FULL JOIN stock_balances b ON b.location_id = t.location_id AND b.product_id = t.product_id
                WHERE COALESCE(t.qty, 0) <> COALESCE(b.qty, 0)
                """
            )
        )
    ).all()
    return [tuple(r) for r in rows]
