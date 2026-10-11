"""Виды работ и тарифы.

Тариф выбирается на дату выполнения работы по приоритету:
сотрудник+подразделение > сотрудник (все подразделения) > подразделение > общий.
Внутри одного уровня действует тариф с самой поздней датой начала (при равенстве — созданный позже).
"""

import datetime as dt
from decimal import Decimal

from sqlalchemy import and_, case, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Department, Employee, Tariff, WorkType, WorkTypeDepartment
from ..domain.errors import AccessDenied, NotFound, ValidationError
from ..domain.money import ROUNDING_MODES
from ..domain.permissions import Perm
from . import reference
from .audit import audit
from .settings import Settings

CALC_METHODS = {
    "per_unit": "количество × ставка",
    "fixed": "фиксированная сумма за операцию",
}


async def create_work_type(
    session: AsyncSession, actor, code: str, unit_code: str, calc_method: str, name: str
) -> WorkType:
    actor.require(Perm.WORKTYPE_MANAGE)
    code = code.strip().lower()
    if not code or len(code) > 32 or " " in code:
        raise ValidationError("Код вида работ — одно слово до 32 символов")
    if calc_method not in CALC_METHODS:
        raise ValidationError("Метод расчёта: " + ", ".join(f"{k} ({v})" for k, v in CALC_METHODS.items()))
    await reference.get_unit(session, unit_code)
    if await get_work_type(session, code, required=False):
        raise ValidationError(f"Вид работ {code} уже есть")
    wt = WorkType(
        code=code, name=name.strip() or code, unit_code=unit_code, calc_method=calc_method, created_by=actor.user.id
    )
    session.add(wt)
    await session.flush()
    await audit(
        session,
        actor.user.id,
        "worktype.create",
        "work_type",
        wt.id,
        {"code": code, "unit": unit_code, "method": calc_method, "name": name},
    )
    return wt


async def get_work_type(session: AsyncSession, code: str, required: bool = True) -> WorkType | None:
    wt = (await session.execute(select(WorkType).where(WorkType.code == code.strip().lower()))).scalar_one_or_none()
    if wt is None and required:
        raise NotFound(f"Нет вида работ «{code}». Список: /wt")
    return wt


async def set_work_type_active(session: AsyncSession, actor, wt: WorkType, active: bool) -> None:
    actor.require(Perm.WORKTYPE_MANAGE)
    wt.is_active = active
    await audit(session, actor.user.id, "worktype.active", "work_type", wt.id, {"active": active})


async def rename_work_type(session: AsyncSession, actor, wt: WorkType, name: str) -> None:
    actor.require(Perm.WORKTYPE_MANAGE)
    old = wt.name
    wt.name = name.strip()
    await audit(session, actor.user.id, "worktype.rename", "work_type", wt.id, {"old": old, "new": wt.name})


async def set_work_type_rounding(
    session: AsyncSession, actor, wt: WorkType, step: Decimal | None, mode: str | None
) -> None:
    actor.require(Perm.WORKTYPE_MANAGE)
    if step is not None and step <= 0:
        raise ValidationError("Шаг округления должен быть больше нуля")
    if mode is not None and mode.upper() not in ROUNDING_MODES:
        raise ValidationError("Режим округления: " + ", ".join(ROUNDING_MODES))
    wt.rounding_step = step
    wt.rounding_mode = mode.upper() if mode else None
    await audit(session, actor.user.id, "worktype.rounding", "work_type", wt.id, {"step": step, "mode": mode})


async def set_work_type_departments(
    session: AsyncSession, actor, wt: WorkType, departments: list[Department] | None
) -> None:
    """departments=None — вид работ доступен во всех подразделениях."""
    actor.require(Perm.WORKTYPE_MANAGE)
    await session.execute(delete(WorkTypeDepartment).where(WorkTypeDepartment.work_type_id == wt.id))
    wt.all_departments = departments is None
    for dept in departments or []:
        session.add(WorkTypeDepartment(work_type_id=wt.id, department_id=dept.id))
    await audit(
        session,
        actor.user.id,
        "worktype.departments",
        "work_type",
        wt.id,
        {"departments": None if departments is None else [d.code for d in departments]},
    )


async def work_type_allowed(session: AsyncSession, wt: WorkType, department_id: int) -> bool:
    if wt.all_departments:
        return True
    found = await session.get(WorkTypeDepartment, (wt.id, department_id))
    return found is not None


async def work_types_for_department(session: AsyncSession, department_id: int) -> list[WorkType]:
    query = (
        select(WorkType)
        .outerjoin(
            WorkTypeDepartment,
            and_(WorkTypeDepartment.work_type_id == WorkType.id, WorkTypeDepartment.department_id == department_id),
        )
        .where(WorkType.is_active, or_(WorkType.all_departments, WorkTypeDepartment.department_id.isnot(None)))
        .order_by(WorkType.name)
    )
    return list((await session.execute(query)).scalars())


async def list_work_types(session: AsyncSession) -> list[WorkType]:
    return list((await session.execute(select(WorkType).order_by(WorkType.is_active.desc(), WorkType.code))).scalars())


async def create_tariffs(
    session: AsyncSession,
    actor,
    settings: Settings,
    *,
    work_type: WorkType,
    rate: Decimal,
    currency: str | None,
    department: Department | None,
    employees: list[Employee] | None,
    valid_from: dt.date | None,
    comment: str | None = None,
) -> list[Tariff]:
    """Создаёт тариф (или по одному на каждого сотрудника из списка). Старые тарифы не меняются."""
    department_id = department.id if department else None
    actor.require(Perm.TARIFF_MANAGE, department_id)
    today = settings.today()
    valid_from = valid_from or today
    if valid_from < today and not actor.can(Perm.TARIFF_BACKDATE):
        raise AccessDenied("Тариф задним числом может создать только владелец или администратор с полными правами")
    if rate < 0:
        raise ValidationError("Ставка не может быть отрицательной")
    currency_code = (currency or settings.currency).upper()
    await reference.get_currency(session, currency_code)
    created = []
    for employee in employees or [None]:
        tariff = Tariff(
            work_type_id=work_type.id,
            department_id=department_id,
            employee_id=employee.id if employee else None,
            rate=rate,
            currency=currency_code,
            valid_from=valid_from,
            comment=comment,
            created_by=actor.user.id,
        )
        session.add(tariff)
        created.append(tariff)
    await session.flush()
    for tariff in created:
        await audit(
            session,
            actor.user.id,
            "tariff.create",
            "tariff",
            tariff.id,
            {
                "work_type": work_type.code,
                "rate": rate,
                "currency": currency_code,
                "department_id": department_id,
                "employee_id": tariff.employee_id,
                "valid_from": valid_from,
                "backdated": valid_from < today,
            },
        )
    return created


def _specificity():
    return case(
        (and_(Tariff.employee_id.isnot(None), Tariff.department_id.isnot(None)), 4),
        (Tariff.employee_id.isnot(None), 3),
        (Tariff.department_id.isnot(None), 2),
        else_=1,
    )


async def resolve_tariff(
    session: AsyncSession, work_type_id: int, department_id: int, employee_id: int, on_date: dt.date
) -> Tariff | None:
    query = (
        select(Tariff)
        .where(
            Tariff.work_type_id == work_type_id,
            Tariff.valid_from <= on_date,
            or_(Tariff.department_id.is_(None), Tariff.department_id == department_id),
            or_(Tariff.employee_id.is_(None), Tariff.employee_id == employee_id),
        )
        .order_by(_specificity().desc(), Tariff.valid_from.desc(), Tariff.id.desc())
        .limit(1)
    )
    return (await session.execute(query)).scalar_one_or_none()


async def tariff_history(session: AsyncSession, work_type_id: int) -> list[tuple[Tariff, str | None, str | None]]:
    rows = (
        await session.execute(
            select(Tariff, Department.code, Employee.code)
            .outerjoin(Department, Department.id == Tariff.department_id)
            .outerjoin(Employee, Employee.id == Tariff.employee_id)
            .where(Tariff.work_type_id == work_type_id)
            .order_by(Tariff.valid_from.desc(), Tariff.id.desc())
        )
    ).all()
    return [(t, d, e) for t, d, e in rows]
