"""Оклад: фиксированная зарплата в месяц (например, у администратора).

Оклад хранится как личная цена сотрудника на служебную работу «Оклад» (за 1 мес). В каждый день зарплаты
(конец расчётного периода: N-е число и конец месяца) начисляется половина: операция «Оклад × 0,5 мес»
проводится автоматически. Поэтому оклад виден в расчёте, в выплатах и отчётах так же, как сдельная работа,
а авансы, премии и штрафы работают как обычно.
"""

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Department, Employee, Tariff, WorkEntry, WorkType
from ..domain.errors import AccessDenied, ValidationError
from ..domain.permissions import Perm
from . import access, ledger, org, tariffs, work
from .audit import audit
from .settings import Settings

CODE = "oklad"
HALF = Decimal("0.5")


async def work_type(session: AsyncSession) -> WorkType | None:
    return (await session.execute(select(WorkType).where(WorkType.code == CODE))).scalar_one_or_none()


async def monthly(session: AsyncSession, settings: Settings, employee: Employee) -> Tariff | None:
    """Действующий оклад сотрудника (личная цена на работу «Оклад»), если он задан и больше нуля."""
    wt = await work_type(session)
    if wt is None:
        return None
    dept_id = await org.default_department_id(session, employee.id)
    tariff = await tariffs.resolve_tariff(session, wt.id, dept_id, employee.id, settings.today())
    return tariff if tariff is not None and tariff.employee_id == employee.id and tariff.rate > 0 else None


async def set_monthly(session: AsyncSession, actor, settings: Settings, employee: Employee, amount: Decimal) -> None:
    """Задаёт оклад в месяц (0 убирает). Действует с текущего периода."""
    if not actor.can_anywhere(Perm.TARIFF_MANAGE):
        raise AccessDenied("Оклад задаёт владелец или руководитель")
    if amount < 0:
        raise ValidationError("Оклад не может быть отрицательным")
    wt = await work_type(session)
    if wt is None:
        wt = await tariffs.create_work_type(session, actor, CODE, "мес", "per_unit", "Оклад")
    dept = None
    if not actor.can(Perm.TARIFF_MANAGE):
        # Руководитель своей точки задаёт оклад в рамках своего подразделения.
        dept_id = await org.default_department_id(session, employee.id)
        dept = await session.get(Department, dept_id) if dept_id else None
    period = await ledger.period_for_date(session, settings.today(), settings)
    # Владелец задаёт оклад с начала текущего периода, остальные с сегодняшнего дня (задним числом нельзя).
    start = period.date_from if actor.can(Perm.TARIFF_BACKDATE, dept.id if dept else None) else settings.today()
    await tariffs.create_tariffs(
        session,
        actor,
        settings,
        work_type=wt,
        rate=amount,
        currency=None,
        department=dept,
        employees=[employee],
        valid_from=start,
    )
    await audit(session, actor.user.id, "payroll.fixed_salary", "employee", employee.id, {"monthly": amount})


async def accrue(session: AsyncSession, settings: Settings, employee: Employee) -> int:
    """Начисляет половину оклада за каждый прошедший день зарплаты, если ещё не начислено. Повтор безопасен."""
    wt = await work_type(session)
    if wt is None:
        return 0
    first = await session.scalar(
        select(func.min(Tariff.valid_from)).where(Tariff.work_type_id == wt.id, Tariff.employee_id == employee.id)
    )
    if first is None:
        return 0
    dept_id = await org.default_department_id(session, employee.id)
    if dept_id is None:
        ids = await org.employee_department_ids(session, employee.id)
        if not ids:
            return 0
        dept_id = ids[0]
    limit = settings.today()
    if not employee.is_active and employee.dismissed_at is not None:
        limit = min(limit, employee.dismissed_at.date())
    created = 0
    period = await ledger.period_for_date(session, first, settings)
    while period.date_to <= limit:
        if await _accrue_period(session, settings, employee, wt, dept_id, period):
            created += 1
        period = await ledger.period_for_date(session, period.date_to + dt.timedelta(days=1), settings)
    return created


async def _accrue_period(session, settings, employee, wt, dept_id, period) -> bool:
    key = f"oklad:{employee.id}:{period.date_from:%Y%m%d}"
    if (await session.execute(select(WorkEntry.id).where(WorkEntry.idempotency_key == key))).first():
        return False
    tariff = await tariffs.resolve_tariff(session, wt.id, dept_id, employee.id, period.date_to)
    if tariff is None or tariff.employee_id != employee.id or tariff.rate <= 0:
        return False
    price = await work.price_entry(
        session,
        settings,
        wt,
        department_id=dept_id,
        employee_id=employee.id,
        performed_on=period.date_to,
        quantity=HALF,
        tariff=tariff,
    )
    author = tariff.created_by or (await access.owners(session))[0].id
    entry = WorkEntry(
        idempotency_key=key,
        department_id=dept_id,
        employee_id=employee.id,
        work_type_id=wt.id,
        performed_on=period.date_to,
        quantity=HALF,
        unit_code=wt.unit_code,
        tariff_id=tariff.id,
        rate=tariff.rate,
        currency=tariff.currency,
        amount=price.amount,
        coefficient=price.coefficient,
        calc_details=price.details,
        status="confirmed",
        comment=f"оклад за {period.title}",
        created_by=author,
        decided_by=author,
        decided_at=func.now(),
    )
    session.add(entry)
    await session.flush()
    await ledger.post(
        session,
        settings,
        employee_id=employee.id,
        department_id=dept_id,
        currency=entry.currency,
        amount=entry.amount,
        entry_type="work",
        source_type="work_entry",
        source_id=entry.id,
        business_date=period.date_to,
        posted_by=author,
        comment=f"Оклад за {period.title}",
    )
    return True


async def accrue_all(session: AsyncSession, settings: Settings) -> int:
    wt = await work_type(session)
    if wt is None:
        return 0
    ids = (await session.execute(select(Tariff.employee_id).where(Tariff.work_type_id == wt.id).distinct())).scalars()
    total = 0
    for emp_id in ids:
        employee = await session.get(Employee, emp_id)
        if employee is not None:
            total += await accrue(session, settings, employee)
    return total
