from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from bugalter.domain.errors import AccessDenied, InsufficientStock, ValidationError
from bugalter.services import ledger, payroll, stock

from .conftest import key

D = Decimal


async def coffee(w):
    product = await stock.create_product(w.session, w.owner, "coffee", "кг", "Кофе зерно")
    await stock.add_product_unit(w.session, w.owner, product, "уп", D("0.25"))
    return product


async def emp_qty(session, w, dept, emp, product):
    loc = await stock.location(session, "employee", dept.id, emp.id)
    return await stock.balance(session, loc.id, product.id)


async def wh_qty(session, dept, product):
    loc = await stock.location(session, "warehouse", dept.id)
    return await stock.balance(session, loc.id, product.id)


async def test_coffee_scenario_with_shortage(world, session, settings):
    """Пример 5 проектирования: поступление → выдача → доставка → возврат → инвентаризация → недостача 144,00."""
    w = world
    product = await coffee(w)
    op = w.operator
    await stock.receive(session, op, key=key(), department=w.north, product=product, qty=D(100))
    issued = await stock.issue(
        session, op, key=key(), department=w.north, employee=w.ivan, product=product, qty=D(40), unit="уп"
    )
    assert issued.document.status == "in_transit" and issued.qty_base == D("10")
    assert await wh_qty(session, w.north, product) == D(90)
    assert await emp_qty(session, w, w.north, w.ivan, product) == 0  # пока не подтвердил получение
    doc, changed = await stock.accept(session, w.ivan_actor, issued.document.id)
    assert changed and doc.status == "completed"
    _, changed = await stock.accept(session, w.ivan_actor, issued.document.id)
    assert not changed
    assert await emp_qty(session, w, w.north, w.ivan, product) == D(10)

    await stock.deliver(
        session,
        op,
        key=key(),
        department=w.north,
        employee=w.ivan,
        product=product,
        qty=D(32),
        unit="уп",
        order_ref="A-1052",
    )
    await stock.return_to_stock(
        session, op, key=key(), department=w.north, employee=w.ivan, product=product, qty=D(6), unit="уп"
    )
    assert await wh_qty(session, w.north, product) == D("91.5")
    assert await emp_qty(session, w, w.north, w.ivan, product) == D("0.5")

    inv = await stock.inventory(
        session, w.manager, key=key(), department=w.north, product=product, counted=D(1), unit="уп", employee=w.ivan
    )
    assert inv.expected == D("0.5") and inv.counted == D("0.25")
    case = inv.case
    assert case.kind == "shortage" and case.qty == D("0.25") and case.status == "open"
    assert await emp_qty(session, w, w.north, w.ivan, product) == D("0.25")
    assert await ledger.balances(session, w.ivan.id) == {}  # недостача сама не становится удержанием

    with pytest.raises(ValidationError, match="основание"):
        await stock.decide_case(session, w.manager, settings, case.id, decision="employee", price=D(576))
    case, adjustment = await stock.decide_case(
        session, w.manager, settings, case.id, decision="employee", price=D(576), price_basis="закупочная цена"
    )
    assert case.amount == D("144.00") and adjustment.status == "pending" and adjustment.kind == "deduction"
    assert await ledger.balances(session, w.ivan.id) == {}  # ждёт подписи
    await payroll.sign_adjustment(session, w.owner, settings, adjustment.id)
    assert await ledger.balances(session, w.ivan.id) == {"USD": D("-144.00")}
    assert await stock.verify_balances(session) == []


async def test_stock_validation(world, session):
    w = world
    product = await coffee(w)
    mugs = await stock.create_product(session, w.owner, "mug", "шт", "Кружка")
    await stock.receive(session, w.operator, key=key(), department=w.north, product=mugs, qty=D(5))
    with pytest.raises(ValidationError):
        await stock.receive(session, w.operator, key=key(), department=w.north, product=mugs, qty=D("2.5"))
    with pytest.raises(ValidationError, match="нет единицы"):
        await stock.receive(session, w.operator, key=key(), department=w.north, product=mugs, qty=D(1), unit="кг")
    with pytest.raises(ValidationError):  # 0,25 × 0,0001 уп = слишком точно для кг (3 знака)
        await stock.receive(
            session, w.operator, key=key(), department=w.north, product=product, qty=D("0.001"), unit="уп"
        )
    with pytest.raises(InsufficientStock):
        await stock.issue(session, w.operator, key=key(), department=w.north, employee=w.ivan, product=mugs, qty=D(6))
    with pytest.raises(AccessDenied):
        await stock.receive(session, w.ivan_actor, key=key(), department=w.north, product=mugs, qty=D(1))
    with pytest.raises(AccessDenied):  # оператор «Севера» не работает со складом «Юга»
        await stock.receive(session, w.operator, key=key(), department=w.south, product=mugs, qty=D(1))


async def test_idempotent_documents(world, session):
    w = world
    mugs = await stock.create_product(session, w.owner, "mug", "шт", "Кружка")
    k = key()
    first = await stock.receive(session, w.operator, key=k, department=w.north, product=mugs, qty=D(5))
    second = await stock.receive(session, w.operator, key=k, department=w.north, product=mugs, qty=D(5))
    assert first.created and not second.created and first.document.id == second.document.id
    assert await wh_qty(session, w.north, mugs) == D(5)


async def test_decline_and_department_transfer(world, session):
    w = world
    mugs = await stock.create_product(session, w.owner, "mug", "шт", "Кружка")
    await stock.receive(session, w.operator, key=key(), department=w.north, product=mugs, qty=D(10))
    issued = await stock.issue(
        session, w.operator, key=key(), department=w.north, employee=w.petr, product=mugs, qty=D(3)
    )
    with pytest.raises(AccessDenied):  # Иван не получатель и без права подтверждения
        await stock.accept(session, w.ivan_actor, issued.document.id)
    await stock.decline(session, w.operator, issued.document.id, "не забрал")
    assert await wh_qty(session, w.north, mugs) == D(10)

    sent = await stock.send(session, w.operator, key=key(), department=w.north, target=w.south, product=mugs, qty=D(4))
    with pytest.raises(AccessDenied):  # оператор «Севера» не принимает за «Юг»
        await stock.accept(session, w.operator, sent.document.id)
    await stock.accept(session, w.admin, sent.document.id)
    assert await wh_qty(session, w.north, mugs) == D(6)
    assert await wh_qty(session, w.south, mugs) == D(4)
    assert await stock.verify_balances(session) == []


async def test_surplus_and_company_decision(world, session, settings):
    w = world
    mugs = await stock.create_product(session, w.owner, "mug", "шт", "Кружка")
    await stock.receive(session, w.operator, key=key(), department=w.north, product=mugs, qty=D(10))
    inv = await stock.inventory(session, w.manager, key=key(), department=w.north, product=mugs, counted=D(12))
    assert inv.case.kind == "surplus"
    assert await wh_qty(session, w.north, mugs) == D(12)
    with pytest.raises(ValidationError):
        await stock.decide_case(
            session, w.manager, settings, inv.case.id, decision="employee", price=D(1), price_basis="x"
        )
    case, adj = await stock.decide_case(session, w.manager, settings, inv.case.id, decision="company")
    assert case.status == "resolved" and adj is None


async def test_moves_are_append_only(world, session):
    w = world
    mugs = await stock.create_product(session, w.owner, "mug", "шт", "Кружка")
    await stock.receive(session, w.operator, key=key(), department=w.north, product=mugs, qty=D(1))
    await session.commit()
    with pytest.raises(DBAPIError, match="только добавлением"):
        await session.execute(text("UPDATE stock_moves SET qty = 100"))
    await session.rollback()
    with pytest.raises(DBAPIError):
        await session.execute(text("UPDATE stock_balances SET qty = -1"))  # остаток не может уйти в минус
    await session.rollback()
