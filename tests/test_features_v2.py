"""Формулы оплаты (ступени, процент, коэффициенты), лимит удержаний, авансы в долг, партии и себестоимость."""

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from bugalter.db.models import AdjustmentInstallment
from bugalter.domain.errors import InsufficientFunds, InsufficientStock, ValidationError
from bugalter.services import ledger, payroll, stock, tariffs, work

from .conftest import key

D = Decimal


def d(day: int, month: int = 10) -> dt.date:
    return dt.date(2026, month, day)


async def op(w, settings, emp, wt, qty, day, confirm=True, coefs=None):
    entry = (
        await work.create_entry(
            w.session,
            w.operator,
            settings,
            idempotency_key=key("op"),
            department_id=w.north.id,
            employee_id=emp.id,
            work_type_id=wt.id,
            quantity=D(qty),
            performed_on=day,
            coefficient_codes=coefs,
        )
    ).entry
    if confirm:
        await work.confirm_entry(w.session, w.owner, settings, entry.id)
    return entry


# ------------------------------------------------------------------ формулы


def test_split_by_tiers():
    tiers = [(D(50), D(450)), (D(100), D(500))]
    assert tariffs.split_by_tiers(D(0), D(10), D(400), tiers) == [(D(10), D(400))]
    assert tariffs.split_by_tiers(D(45), D(10), D(400), tiers) == [(D(5), D(400)), (D(5), D(450))]
    assert tariffs.split_by_tiers(D(40), D(70), D(400), tiers) == [(D(10), D(400)), (D(50), D(450)), (D(10), D(500))]
    assert tariffs.split_by_tiers(D(120), D(1), D(400), tiers) == [(D(1), D(500))]


async def test_tiered_tariff_accumulates_within_period(world, session, settings):
    w = world
    await w.tariff(w.delivery, "400", d(16), dept=w.north)
    tariff = (await tariffs.tariff_history(session, w.delivery.id))[0][0]
    # тарифы неизменяемы — ступени задаются при создании
    await tariffs.create_tariffs(
        session,
        w.owner,
        settings,
        work_type=w.delivery,
        rate=D(400),
        currency="USD",
        department=w.north,
        employees=None,
        valid_from=d(16),
        tiers=[(D(50), D(450))],
    )
    assert tariff.id != (await tariffs.tariff_history(session, w.delivery.id))[0][0].id
    first = await op(w, settings, w.ivan, w.delivery, "45", d(17))
    assert first.amount == D("18000.00")
    second = await op(w, settings, w.ivan, w.delivery, "10", d(18))
    assert second.amount == D(5 * 400 + 5 * 450)
    assert [p["rate"] for p in second.calc_details["pieces"]] == ["400", "450"]
    # другой сотрудник начинает с первой ступени
    other = await op(w, settings, w.petr, w.delivery, "10", d(18))
    assert other.amount == D(4000)
    # новый период — накопление с нуля
    nov = await op(w, settings, w.ivan, w.delivery, "1", d(26))
    assert nov.amount == D(450)  # 26.10 — тот же период 16–31.10, накоплено 55
    st = await payroll.build_statement(session, w.ivan, date_from=d(16), date_to=d(31))
    lines = {(line.rate, line.tiered): (line.quantity, line.amount) for line in st.currencies["USD"].work_lines}
    assert lines == {(D(400), True): (D(50), D(20000)), (D(450), True): (D(6), D(2700))}
    assert sum(line.amount for line in st.currencies["USD"].work_lines) == st.currencies["USD"].accrued


async def test_tiers_are_assigned_by_confirmation_order(world, session, settings):
    """Ступень определяется подтверждённым объёмом на момент подтверждения, а не ввода."""
    w = world
    await tariffs.create_tariffs(
        session,
        w.owner,
        settings,
        work_type=w.delivery,
        rate=D(400),
        currency="USD",
        department=w.north,
        employees=None,
        valid_from=d(16),
        tiers=[(D(10), D(500))],
    )
    a = await op(w, settings, w.ivan, w.delivery, "8", d(17), confirm=False)
    b = await op(w, settings, w.ivan, w.delivery, "8", d(17), confirm=False)
    assert a.amount == b.amount == D(3200)  # предварительно — от нуля
    await work.confirm_entry(session, w.owner, settings, b.id)
    await work.confirm_entry(session, w.owner, settings, a.id)
    assert b.amount == D(3200)
    assert a.amount == D(2 * 400 + 6 * 500)


async def test_percent_and_fixed_methods(world, session, settings):
    w = world
    commission = await tariffs.create_work_type(session, w.owner, "комиссия", "сумма", "percent", "Комиссия с заказа")
    await w.tariff(commission, "3.5", d(1), dept=w.north)
    entry = await op(w, settings, w.ivan, commission, "12345.67", d(20))
    assert entry.amount == D("432.10")  # 12 345,67 × 3,5 % = 432,09845
    loading = await tariffs.create_work_type(session, w.owner, "погрузка", "шт", "fixed", "Погрузка машины")
    await w.tariff(loading, "700", d(1), dept=w.north)
    entry = await op(w, settings, w.ivan, loading, "3", d(20))
    assert entry.amount == D(700)
    with pytest.raises(ValidationError, match="per_unit"):
        await tariffs.create_tariffs(
            session,
            w.owner,
            settings,
            work_type=commission,
            rate=D(1),
            currency="USD",
            department=None,
            employees=None,
            valid_from=d(26),
            tiers=[(D(10), D(2))],
        )


async def test_coefficients(world, session, settings):
    w = world
    await w.tariff(w.delivery, "400", d(1), dept=w.north)
    await tariffs.create_coefficient(session, w.owner, "срочно", D("1.5"), "Срочный заказ")
    await tariffs.create_coefficient(session, w.owner, "выходной", D("2"), "Работа в выходной")
    entry = await op(w, settings, w.ivan, w.delivery, "3", d(20), coefs=["*срочно", "выходной"])
    assert entry.amount == D(3 * 400 * 3) and entry.coefficient == D(3)
    assert entry.coefficient_codes == ["срочно", "выходной"]
    with pytest.raises(ValidationError, match="коэффициента"):
        await op(w, settings, w.ivan, w.delivery, "1", d(20), coefs=["нет_такого"])
    await tariffs.set_coefficient_active(session, w.owner, "срочно", False)
    with pytest.raises(ValidationError):
        await op(w, settings, w.ivan, w.delivery, "1", d(20), coefs=["срочно"])
    st = await payroll.build_statement(session, w.ivan, date_from=d(16), date_to=d(31))
    assert st.currencies["USD"].work_lines[0].coefficient == D(3)
    # коэффициент подтверждённой операции не меняется
    await session.commit()
    with pytest.raises(DBAPIError):
        await session.execute(text("UPDATE work_entries SET coefficient = 1 WHERE id = :id"), {"id": entry.id})
    await session.rollback()


# ------------------------------------------------------------------ лимит удержаний


async def test_deduction_limit_withholds_in_installments(world, session, settings):
    w = world
    limited = dataclasses.replace(settings, deduction_limit_percent=D(20))
    await w.tariff(w.delivery, "100", d(1), dept=w.north)
    await op(w, limited, w.petr, w.delivery, "10", d(20))  # начислено 1000
    fine, _ = await payroll.create_adjustment(
        session,
        w.manager,
        limited,
        kind="deduction",
        employee=w.petr,
        amount=D(500),
        currency="USD",
        reason="недостача",
        idempotency_key=key(),
        department_id=w.north.id,
    )
    fine, posted, _ = await payroll.sign_adjustment(session, w.owner, limited, fine.id)
    assert fine.status == "approved" and posted.amount == D(-200)  # 20 % от 1000
    assert fine.withheld == D(200) and fine.outstanding == D(300)
    assert (await ledger.balances(session, w.petr.id))["USD"] == D(800)
    # новые начисления в том же периоде открывают место для следующей части
    await op(w, limited, w.petr, w.delivery, "5", d(21))  # +500 → лимит 300, удержано 200 → ещё 100
    await session.refresh(fine)
    assert fine.withheld == D(300)
    # следующий период: удержание продолжается от начислений нового периода
    nov = dataclasses.replace(limited, today_override=dt.date(2026, 11, 3))
    await op(w, nov, w.petr, w.delivery, "20", dt.date(2026, 11, 2))  # 2000 → лимит 400 → остаток 200
    await session.refresh(fine)
    assert fine.withheld == D(500) and fine.outstanding == 0
    installments = (
        (await session.execute(select(AdjustmentInstallment).where(AdjustmentInstallment.adjustment_id == fine.id)))
        .scalars()
        .all()
    )
    assert [i.amount for i in installments] == [D(200), D(100), D(200)]
    assert (await ledger.balances(session, w.petr.id))["USD"] == D(3000)
    st = await payroll.build_statement(session, w.petr, date_from=d(1), date_to=d(31), include_reserved=True)
    assert st.currencies["USD"].outstanding_withholding == 0
    # отмена удержания возвращает всё удержанное
    _, reversal = await payroll.cancel_adjustment(session, w.owner, limited, fine.id, "ошибка")
    assert reversal is not None
    assert (await ledger.balances(session, w.petr.id))["USD"] == D(3500)


async def test_without_limit_deduction_is_posted_at_once(world, session, settings):
    w = world
    fine, _ = await payroll.create_adjustment(
        session,
        w.manager,
        settings,
        kind="penalty",
        employee=w.petr,
        amount=D(300),
        currency="USD",
        reason="опоздание",
        idempotency_key=key(),
        department_id=w.north.id,
    )
    fine, posted, _ = await payroll.sign_adjustment(session, w.owner, settings, fine.id)
    assert posted.amount == D(-300) and fine.outstanding == 0


# ------------------------------------------------------------------ аванс и зарплата


async def test_advance_any_amount_and_deduction_at_payday(world, session, settings):
    """Аванс любой суммы; в день зарплаты руководитель вычитает его сейчас или оставляет на потом."""
    w = world
    await w.tariff(w.delivery, "50", d(1), dept=w.north)
    await op(w, settings, w.petr, w.delivery, "4", d(17))  # заработал 200

    async def pay(kind, amount):
        p, _ = await payroll.register_payout(
            session,
            w.owner,
            settings,
            employee=w.petr,
            amount=D(amount),
            currency="USD",
            kind=kind,
            idempotency_key=key(),
        )
        await payroll.confirm_payout(session, w.owner, settings, p.id)

    async def salary(deduct):
        p, deducted = await payroll.pay_salary(
            session, w.owner, settings, employee=w.petr, deduct_advance=deduct, idempotency_key=key(), currency="USD"
        )
        if p is not None:
            await payroll.confirm_payout(session, w.owner, settings, p.id)
        return (p.amount if p else None), deducted

    async def preview():
        pv = await payroll.salary_preview(session, settings, w.petr, "USD")
        return pv.earned, pv.advance, pv.with_deduction

    await pay("advance", "300")  # срочно нужны деньги: больше, чем заработал
    with pytest.raises(InsufficientFunds, match="авансом"):
        await pay("salary", "1")  # зарплатой больше заработанного нельзя
    await op(w, settings, w.petr, w.delivery, "1", d(18))  # всего заработал 250
    assert await preview() == (D(250), D(300), D(0))

    # 15-е число: руководитель решил аванс пока не вычитать
    assert await salary(False) == (D(250), 0)
    assert await preview() == (D(0), D(300), D(0))

    # 30-е: заработал ещё 250, вычитаем аванс; к выплате ничего, 50 аванса остаётся на следующий раз
    await op(w, settings, w.petr, w.delivery, "5", d(20))
    assert await salary(True) == (None, D(250))
    assert await preview() == (D(0), D(50), D(0))

    # Следующая зарплата: заработал 100, вычитается остаток аванса 50
    await op(w, settings, w.petr, w.delivery, "2", d(21))
    assert await preview() == (D(100), D(50), D(50))
    assert await salary(True) == (D(50), D(50))
    assert await preview() == (D(0), D(0), D(0))
    assert (await ledger.balances(session, w.petr.id))["USD"] == D(0)
    with pytest.raises(ValidationError, match="нечего"):
        await salary(True)

    # Сотрудник может попросить аванс больше заработанного: решает руководитель
    req, _ = await payroll.request_payout(
        session, w.ivan_actor, settings, amount=D(1000), currency="USD", comment=None, idempotency_key=key()
    )
    _, payout = await payroll.decide_request(session, w.owner, settings, req.id, True)
    assert payout.kind == "advance" and payout.amount == D(1000)


# ------------------------------------------------------------------ партии и себестоимость


async def test_batches_fifo_and_valuation(world, session, settings):
    w = world
    coffee = await stock.create_product(session, w.owner, "coffee", "кг", "Кофе")
    await stock.add_product_unit(session, w.owner, coffee, "уп", D("0.25"))
    op_ = w.operator
    await stock.receive(
        session,
        op_,
        key=key(),
        department=w.north,
        product=coffee,
        qty=D(10),
        unit_cost=D(5),
        currency="USD",
        received_on=d(1),
    )
    # вторая партия: 40 упаковок по 1,5 = 6 за кг
    await stock.receive(
        session,
        op_,
        key=key(),
        department=w.north,
        product=coffee,
        qty=D(40),
        unit="уп",
        unit_cost=D("1.5"),
        currency="USD",
        received_on=d(5),
    )
    rows = await stock.balances(session, department_id=w.north.id)
    assert rows[0].qty == D(20) and rows[0].value == D(10 * 5 + 10 * 6) and rows[0].unvalued_qty == 0

    issued = await stock.issue(session, op_, key=key(), department=w.north, employee=w.ivan, product=coffee, qty=D(12))
    await stock.accept(session, w.ivan_actor, issued.document.id)
    ivan_rows = await stock.balances(session, employee_id=w.ivan.id)
    # FIFO: 10 кг первой партии по 5 + 2 кг второй по 6
    assert ivan_rows[0].qty == D(12) and ivan_rows[0].value == D(62)
    wh = await stock.balances(session, department_id=w.north.id)
    assert [r.value for r in wh if r.location.kind == "warehouse"] == [D(48)]

    inv = await stock.inventory(
        session, w.manager, key=key(), department=w.north, product=coffee, counted=D(9), employee=w.ivan
    )
    # недостача 3 кг: FIFO с руки Ивана — 3 кг первой партии по 5
    assert await stock.case_cost(session, inv.case) == (D(15), "USD")
    case, adjustment = await stock.decide_case(
        session, w.manager, settings, inv.case.id, decision="employee", use_cost=True
    )
    assert adjustment.amount == D(15) and "себестоимость" in case.price_basis
    batches = await stock.product_batches(session, coffee)
    assert [(b.unit_cost, q) for b, q in batches] == [(D(6), D(10)), (D(5), D(7))]
    assert await stock.verify_balances(session) == []


async def test_transit_returns_exact_batches(world, session):
    w = world
    mug = await stock.create_product(session, w.owner, "mug", "шт", "Кружка")
    await stock.receive(
        session,
        w.operator,
        key=key(),
        department=w.north,
        product=mug,
        qty=D(2),
        unit_cost=D(10),
        currency="USD",
        received_on=d(1),
    )
    await stock.receive(
        session,
        w.operator,
        key=key(),
        department=w.north,
        product=mug,
        qty=D(5),
        unit_cost=D(20),
        currency="USD",
        received_on=d(2),
    )
    first = await stock.issue(
        session, w.operator, key=key(), department=w.north, employee=w.ivan, product=mug, qty=D(2)
    )
    second = await stock.issue(
        session, w.operator, key=key(), department=w.north, employee=w.petr, product=mug, qty=D(3)
    )
    # Пётр принимает раньше Ивана — но получает именно свои партии (по 20), а не первые в «пути»
    await stock.accept(session, w.manager, second.document.id)
    petr = await stock.balances(session, employee_id=w.petr.id)
    assert petr[0].value == D(60)
    await stock.decline(session, w.operator, first.document.id, "не забрал")
    wh = [r for r in await stock.balances(session, department_id=w.north.id) if r.location.kind == "warehouse"]
    assert wh[0].qty == D(4) and wh[0].value == D(2 * 10 + 2 * 20)
    assert await stock.verify_balances(session) == []
    with pytest.raises(InsufficientStock):
        await stock.issue(session, w.operator, key=key(), department=w.north, employee=w.ivan, product=mug, qty=D(5))


async def test_receipt_cost_requires_currency_and_batches_are_append_only(world, session):
    w = world
    mug = await stock.create_product(session, w.owner, "mug", "шт", "Кружка")
    with pytest.raises(ValidationError):
        await stock.receive(session, w.operator, key=key(), department=w.north, product=mug, qty=D(1), unit_cost=D(5))
    await stock.receive(session, w.operator, key=key(), department=w.north, product=mug, qty=D(1))
    await session.commit()
    with pytest.raises(DBAPIError, match="только добавлением"):
        await session.execute(text("UPDATE batches SET unit_cost = 1, currency = 'USD'"))
    await session.rollback()


async def test_fixed_monthly_salary(world, session, settings):
    """Оклад: половина в каждый день зарплаты, сама; вместе с авансом и вычетом."""
    from bugalter.services import fixed_salary

    w = world
    await fixed_salary.set_monthly(session, w.owner, settings, w.petr, D(300))
    assert (await fixed_salary.monthly(session, settings, w.petr)).rate == D(300)
    # 26.10: день зарплаты 31.10 ещё не наступил
    assert (await payroll.salary_preview(session, settings, w.petr)).earned == 0
    payday = dataclasses.replace(settings, today_override=d(31))
    assert (await payroll.salary_preview(session, payday, w.petr)).earned == D(150)
    assert await fixed_salary.accrue(session, payday, w.petr) == 0  # повтор ничего не начисляет
    st = await payroll.build_statement(session, w.petr, date_from=d(16), date_to=d(31))
    assert [(line.title, line.amount) for line in st.currencies["USD"].work_lines] == [("Оклад", D(150))]
    # аванс 100 и зарплата с вычетом
    adv, _ = await payroll.register_payout(
        session, w.owner, payday, employee=w.petr, amount=D(100), currency="USD", kind="advance", idempotency_key=key()
    )
    await payroll.confirm_payout(session, w.owner, payday, adv.id)
    payout, deducted = await payroll.pay_salary(
        session, w.owner, payday, employee=w.petr, deduct_advance=True, idempotency_key=key()
    )
    assert payout.amount == D(50) and deducted == D(100)
    # 15.11: ещё половина
    nov = dataclasses.replace(settings, today_override=dt.date(2026, 11, 15))
    assert await fixed_salary.accrue(session, nov, w.petr) == 1
    # служебная работа не видна в тегах и списке работ
    assert "оклад" not in await tariffs.work_types_by_tag(session)
