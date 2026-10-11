import dataclasses
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from bugalter.db.models import PeriodStatement
from bugalter.domain.errors import AccessDenied, Conflict, InsufficientFunds, ValidationError
from bugalter.services import ledger, payroll, work

from .conftest import key

D = Decimal


def d(day: int, month: int = 10) -> dt.date:
    return dt.date(2026, month, day)


async def op(w, settings, emp, wt, qty, day, confirm=True):
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
        )
    ).entry
    if confirm:
        await work.confirm_entry(w.session, w.manager, settings, entry.id)
    return entry


async def adj(w, settings, kind, emp, amount, reason="основание", actor=None):
    a, _ = await payroll.create_adjustment(
        w.session,
        actor or w.manager,
        settings,
        kind=kind,
        employee=emp,
        amount=D(amount),
        currency="USD",
        reason=reason,
        idempotency_key=key("adj"),
        department_id=w.north.id,
    )
    return a


async def test_ivan_statement_example(world, session, settings):
    """Пример 4 проектирования: расчёт Ивана за 16.10–31.10."""
    w = world
    await w.tariff(w.delivery, "350", d(1), dept=w.north)
    await w.tariff(w.delivery, "400", d(15), dept=w.north, employees=[w.ivan])
    await w.tariff(w.delivery, "420", d(20), dept=w.north, employees=[w.ivan])
    await w.tariff(w.packing, "20", d(1), dept=w.north)
    # Период 1–15: доставка 14.10 (позже окажется дублем)
    dup = await op(w, settings, w.ivan, w.delivery, "1", d(14))
    p1 = await ledger.period_for_date(session, d(14), settings)
    payout1, _ = await payroll.register_payout(
        session,
        w.accountant,
        settings,
        employee=w.ivan,
        amount=D("350"),
        currency="USD",
        kind="salary",
        idempotency_key=key("p"),
        department_id=w.north.id,
        business_date=d(15),
    )
    await payroll.confirm_payout(session, w.manager, settings, payout1.id)
    await payroll.close_period(session, w.owner, settings, p1.id)

    await op(w, settings, w.ivan, w.delivery, "30", d(17))
    await op(w, settings, w.ivan, w.delivery, "25", d(22))
    for qty in ("3", "2.5", "3"):
        await op(w, settings, w.ivan, w.packing, qty, d(18))
    bonus = await adj(w, settings, "bonus", w.ivan, "1000", "выполнение плана")
    await payroll.sign_adjustment(session, w.owner, settings, bonus.id)
    _, reversal = await work.cancel_entry(session, w.manager, settings, dup.id, "дубль")
    deduction = await adj(w, settings, "deduction", w.ivan, "144", "недостача Р-7")
    await payroll.sign_adjustment(session, w.owner, settings, deduction.id)
    advance, _ = await payroll.register_payout(
        session,
        w.accountant,
        settings,
        employee=w.ivan,
        amount=D("5000"),
        currency="USD",
        kind="advance",
        idempotency_key=key("p"),
        business_date=d(20),
        department_id=w.north.id,
    )
    await payroll.confirm_payout(session, w.manager, settings, advance.id)
    await op(w, settings, w.ivan, w.delivery, "2", d(26), confirm=False)

    p2 = await ledger.period_for_date(session, d(20), settings)
    assert reversal.period_id == p2.id and reversal.business_date == d(14)  # сторно — в открытом периоде
    st = await payroll.build_statement(session, w.ivan, period=p2, include_reserved=True)
    usd = st.currencies["USD"]
    assert usd.opening == D("0")
    assert usd.accrued == D("23670.00")
    assert usd.deducted == D("-144.00")
    assert usd.corrections == D("-350.00")
    assert usd.paid == D("-5000.00")
    assert usd.closing == D("18176.00")
    assert (usd.pending_count, usd.pending_amount) == (1, D("840.00"))
    assert sum(line.amount for line in usd.work_lines) == D("22670.00")
    lines = {(line.title, line.rate): (line.count, line.quantity, line.amount) for line in usd.work_lines}
    assert lines == {
        ("Доставка", D("400")): (1, D("30"), D("12000.00")),
        ("Доставка", D("420")): (1, D("25"), D("10500.00")),
        ("Фасовка", D("20")): (3, D("8.5"), D("170.00")),
    }


async def test_bonus_sign_rules(world, session, settings):
    w = world
    bonus = await adj(w, settings, "bonus", w.ivan, "500")
    assert bonus.status == "pending"
    assert await ledger.balances(session, w.ivan.id) == {}
    strict = dataclasses.replace(settings, allow_self_confirm=False)
    with pytest.raises(AccessDenied, match="Автор"):
        await payroll.sign_adjustment(session, w.manager, strict, bonus.id)
    with pytest.raises(AccessDenied):
        await payroll.sign_adjustment(session, w.operator, settings, bonus.id)
    _, posted, changed = await payroll.sign_adjustment(session, w.manager, settings, bonus.id)
    assert changed and posted.amount == D("500")
    _, posted2, changed2 = await payroll.sign_adjustment(session, w.manager, settings, bonus.id)
    assert not changed2 and posted2 is None
    # премию самому себе подписать нельзя
    self_bonus = await adj(w, settings, "bonus", w.anna_emp, "100", actor=w.owner)
    with pytest.raises(AccessDenied, match="себя"):
        await payroll.sign_adjustment(session, w.manager, settings, self_bonus.id)


async def test_adjustment_requires_reason_and_precision(world, settings):
    w = world
    with pytest.raises(ValidationError):
        await adj(w, settings, "bonus", w.ivan, "10", reason=" ")
    with pytest.raises(ValidationError):
        await adj(w, settings, "bonus", w.ivan, "10.005")


async def test_deduction_consent_flow(world, session, settings):
    w = world
    consent = dataclasses.replace(settings, deduction_requires_consent=True)
    fine = await adj(w, settings, "penalty", w.ivan, "200", "опоздание с доставкой")
    fine, posted, _ = await payroll.sign_adjustment(session, w.owner, consent, fine.id)
    assert fine.status == "awaiting_consent" and posted is None
    with pytest.raises(AccessDenied):
        await payroll.consent_adjustment(session, w.manager, consent, fine.id, True)
    fine, posted = await payroll.consent_adjustment(session, w.ivan_actor, consent, fine.id, True)
    assert fine.status == "approved" and posted.amount == D("-200")
    other = await adj(w, settings, "deduction", w.ivan, "50")
    await payroll.sign_adjustment(session, w.owner, consent, other.id)
    other, _ = await payroll.consent_adjustment(session, w.ivan_actor, consent, other.id, False)
    assert other.status == "disputed"
    assert await ledger.balances(session, w.ivan.id) == {"USD": D("-200")}


async def test_payout_cannot_exceed_balance(world, session, settings):
    w = world
    await w.tariff(w.delivery, "100", d(1), dept=w.north)
    await op(w, settings, w.petr, w.delivery, "3", d(20))
    with pytest.raises(InsufficientFunds):
        await payroll.register_payout(
            session,
            w.accountant,
            settings,
            employee=w.petr,
            amount=D("301"),
            currency="USD",
            kind="advance",
            idempotency_key=key(),
        )
    p, _ = await payroll.register_payout(
        session,
        w.accountant,
        settings,
        employee=w.petr,
        amount=D("200"),
        currency="USD",
        kind="advance",
        idempotency_key=key(),
    )
    # зарегистрированная, но не подтверждённая выплата уже резервирует деньги
    with pytest.raises(InsufficientFunds):
        await payroll.register_payout(
            session,
            w.accountant,
            settings,
            employee=w.petr,
            amount=D("150"),
            currency="USD",
            kind="advance",
            idempotency_key=key(),
        )
    loose = dataclasses.replace(settings, allow_negative_balance=True)
    await payroll.register_payout(
        session,
        w.accountant,
        loose,
        employee=w.petr,
        amount=D("150"),
        currency="USD",
        kind="advance",
        idempotency_key=key(),
    )
    strict = dataclasses.replace(settings, allow_self_confirm=False)
    with pytest.raises(AccessDenied):
        await payroll.confirm_payout(session, w.accountant, strict, p.id)
    await payroll.confirm_payout(session, w.manager, strict, p.id)
    assert (await ledger.balances(session, w.petr.id))["USD"] == D("100")


async def test_payout_idempotent_and_self_payout_forbidden(world, session, settings):
    w = world
    await w.tariff(w.processing, "100", d(1), dept=w.north)
    await op(w, settings, w.petr, w.processing, "5", d(20))
    anna_op = await op(w, settings, w.anna_emp, w.processing, "5", d(20), confirm=False)
    await work.confirm_entry(session, w.owner, settings, anna_op.id)
    with pytest.raises(AccessDenied, match="самому себе"):
        await payroll.register_payout(
            session,
            w.manager,
            settings,
            employee=w.anna_emp,
            amount=D("10"),
            currency="USD",
            kind="salary",
            idempotency_key=key(),
        )
    k = key()
    first, created1 = await payroll.register_payout(
        session,
        w.accountant,
        settings,
        employee=w.petr,
        amount=D("100"),
        currency="USD",
        kind="salary",
        idempotency_key=k,
    )
    second, created2 = await payroll.register_payout(
        session,
        w.accountant,
        settings,
        employee=w.petr,
        amount=D("100"),
        currency="USD",
        kind="salary",
        idempotency_key=k,
    )
    assert created1 and not created2 and first.id == second.id


async def test_payout_request_flow(world, session, settings):
    w = world
    await w.tariff(w.delivery, "100", d(1), dept=w.north)
    await op(w, settings, w.ivan, w.delivery, "5", d(20))
    req, created = await payroll.request_payout(
        session, w.ivan_actor, settings, amount=D("300"), currency="USD", comment="на карту", idempotency_key="r1"
    )
    assert created
    _, again = await payroll.request_payout(
        session, w.ivan_actor, settings, amount=D("300"), currency="USD", comment="на карту", idempotency_key="r1"
    )
    assert not again
    with pytest.raises(InsufficientFunds):  # 500 − 300 зарезервировано
        await payroll.request_payout(
            session, w.ivan_actor, settings, amount=D("201"), currency="USD", comment=None, idempotency_key="r2"
        )
    req, payout = await payroll.decide_request(session, w.accountant, settings, req.id, True)
    assert req.status == "approved" and payout.status == "registered" and payout.amount == D("300")
    with pytest.raises(AccessDenied):  # выплата без подразделения — нужно глобальное право
        await payroll.confirm_payout(session, w.manager, settings, payout.id)
    await payroll.confirm_payout(session, w.owner, settings, payout.id)
    assert (await ledger.balances(session, w.ivan.id))["USD"] == D("200")


async def test_close_period_and_guards(world, session, settings):
    w = world
    await w.tariff(w.delivery, "100", d(1), dept=w.north)
    await op(w, settings, w.ivan, w.delivery, "2", d(10))
    await op(w, settings, w.ivan, w.delivery, "1", d(11), confirm=False)
    p1 = await ledger.period_for_date(session, d(10), settings)
    with pytest.raises(AccessDenied):
        await payroll.close_period(session, w.operator, settings, p1.id)
    with pytest.raises(payroll.PendingEntriesError):
        await payroll.close_period(session, w.admin, settings, p1.id)
    period, statements = await payroll.close_period(session, w.admin, settings, p1.id, force=True)
    assert period.status == "closed" and period.version == 1
    assert [(s.employee_id, s.accrued, s.closing) for s in statements] == [(w.ivan.id, D("200"), D("200"))]
    await session.commit()

    # Подтверждение операции из закрытого периода уходит в открытый период с исходной датой
    late = (await work.list_entries(session, employee_id=w.ivan.id, statuses=("pending",)))[0][0]
    _, posted, _ = await work.confirm_entry(session, w.manager, settings, late.id)
    p2 = await ledger.period_for_date(session, d(20), settings)
    assert posted.period_id == p2.id and posted.business_date == d(11)
    await session.commit()

    # Прямая запись в закрытый период запрещена базой
    with pytest.raises(DBAPIError, match="закрыт"):
        await session.execute(
            text(
                "INSERT INTO ledger_entries (employee_id, currency, amount, entry_type, source_type, source_id, "
                "period_id, business_date) VALUES (:e, 'USD', 1, 'bonus', 'manual', 1, :p, '2026-10-10')"
            ),
            {"e": w.ivan.id, "p": p1.id},
        )
    await session.rollback()


async def test_reopen_only_owner_with_reason(world, session, settings):
    w = world
    await w.tariff(w.delivery, "100", d(1), dept=w.north)
    await op(w, settings, w.ivan, w.delivery, "2", d(10))
    p1 = await ledger.period_for_date(session, d(10), settings)
    await payroll.close_period(session, w.admin, settings, p1.id)
    with pytest.raises(AccessDenied):
        await payroll.reopen_period(session, w.admin, p1.id, "ошибка")
    with pytest.raises(ValidationError):
        await payroll.reopen_period(session, w.owner, p1.id, "")
    await payroll.reopen_period(session, w.owner, p1.id, "ошибка в тарифе")
    bonus = await adj(w, settings, "bonus", w.ivan, "50")
    bonus.business_date = d(12)
    await payroll.sign_adjustment(session, w.owner, settings, bonus.id)
    period, _ = await payroll.close_period(session, w.admin, settings, p1.id)
    assert period.version == 2
    rows = (
        (
            await session.execute(
                select(PeriodStatement).where(PeriodStatement.period_id == p1.id).order_by(PeriodStatement.version)
            )
        )
        .scalars()
        .all()
    )
    assert [(r.version, r.closing) for r in rows] == [(1, D("200")), (2, D("250"))]
    assert rows[0].entries_hash != rows[1].entries_hash


async def test_close_requires_earlier_periods_closed(world, session, settings):
    w = world
    await w.tariff(w.delivery, "100", d(1, 9), dept=w.north)
    await op(w, settings, w.ivan, w.delivery, "1", d(10, 9))
    await op(w, settings, w.ivan, w.delivery, "1", d(10))
    p_oct = await ledger.period_for_date(session, d(10), settings)
    with pytest.raises(Conflict, match="более ранние"):
        await payroll.close_period(session, w.admin, settings, p_oct.id)
