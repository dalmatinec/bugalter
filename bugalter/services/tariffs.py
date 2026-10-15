"""Виды работ и тарифы.

Тариф выбирается на дату выполнения работы по приоритету:
сотрудник+подразделение > сотрудник (все подразделения) > подразделение > общий.
Внутри одного уровня действует тариф с самой поздней датой начала (при равенстве — созданный позже).
"""

import datetime as dt
from decimal import Decimal

from sqlalchemy import and_, case, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Coefficient, Department, Employee, Tariff, TariffTier, WorkType, WorkTypeDepartment
from ..domain.errors import AccessDenied, NotFound, ValidationError
from ..domain.money import ROUNDING_MODES, scale_of
from ..domain.permissions import Perm
from . import reference
from .audit import audit
from .settings import Settings

CALC_METHODS = {
    "per_unit": "количество × ставка (можно со ступенчатой шкалой)",
    "fixed": "фиксированная сумма за операцию",
    "percent": "процент от суммы, где количество - это база, например сумма заказа, а ставка - процент",
}


async def create_work_type(
    session: AsyncSession, actor, code: str, unit_code: str, calc_method: str, name: str
) -> WorkType:
    actor.require(Perm.WORKTYPE_MANAGE)
    code = code.strip().lower()
    if not code or len(code) > 32 or " " in code:
        raise ValidationError("Код вида работ - одно слово до 32 символов")
    if calc_method not in CALC_METHODS:
        raise ValidationError("Метод расчёта: " + ", ".join(f"{k} ({v})" for k, v in CALC_METHODS.items()))
    await reference.get_unit(session, unit_code)
    if (await session.execute(select(WorkType.id).where(WorkType.code == code))).first():
        raise ValidationError(f"Вид работ {code} уже есть")
    tag = work_tag(name or code)
    if tag in await work_types_by_tag(session):
        raise ValidationError(f"Работа с тегом #{tag} уже есть, назовите эту иначе")
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
    from .org import lookup

    wt = await lookup(session, WorkType, WorkType.code, WorkType.name, code)
    if wt is None and required:
        raise NotFound(f"Нет такого вида работ: {code}")
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


def work_tag(name: str) -> str:
    """Тег работы для отчёта в группе: название без пробелов и знаков. «Фасовка 0,5» → фасовка05."""
    return "".join(ch for ch in name.lower().replace("ё", "е") if ch.isalnum())


async def work_types_by_tag(session: AsyncSession) -> dict[str, WorkType]:
    types = (await session.execute(select(WorkType).where(WorkType.is_active))).scalars()
    return {work_tag(wt.name): wt for wt in types if work_tag(wt.name)}


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
    tiers: list[tuple[Decimal, Decimal]] | None = None,
) -> list[Tariff]:
    """Создаёт тариф (или по одному на каждого сотрудника из списка). Старые тарифы не меняются.

    tiers — ступени шкалы [(порог, ставка)]: единицы сверх порога (накопленно за период) идут по своей ставке.
    """
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
    tiers = validate_tiers(work_type, tiers)
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
        for threshold, tier_rate in tiers:
            session.add(TariffTier(tariff_id=tariff.id, threshold=threshold, rate=tier_rate))
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
                "tiers": [[t, r] for t, r in tiers],
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
            select(Tariff, Department.name, Employee.full_name)
            .outerjoin(Department, Department.id == Tariff.department_id)
            .outerjoin(Employee, Employee.id == Tariff.employee_id)
            .where(Tariff.work_type_id == work_type_id)
            .order_by(Tariff.valid_from.desc(), Tariff.id.desc())
        )
    ).all()
    return [(t, d, e) for t, d, e in rows]


def validate_tiers(work_type: WorkType, tiers: list[tuple[Decimal, Decimal]] | None) -> list[tuple[Decimal, Decimal]]:
    if not tiers:
        return []
    if work_type.calc_method != "per_unit":
        raise ValidationError("Ступенчатая шкала возможна только для метода per_unit")
    result = sorted(tiers)
    previous = Decimal(0)
    for threshold, rate in result:
        if threshold <= previous:
            raise ValidationError("Пороги шкалы должны быть больше нуля и не повторяться")
        if rate < 0:
            raise ValidationError("Ставка ступени не может быть отрицательной")
        previous = threshold
    return result


async def tiers_of(session: AsyncSession, tariff_id: int) -> list[tuple[Decimal, Decimal]]:
    rows = (
        await session.execute(
            select(TariffTier.threshold, TariffTier.rate)
            .where(TariffTier.tariff_id == tariff_id)
            .order_by(TariffTier.threshold)
        )
    ).all()
    return [(t, r) for t, r in rows]


async def tiers_of_many(session: AsyncSession, tariff_ids: list[int]) -> dict[int, list[tuple[Decimal, Decimal]]]:
    """Ступени сразу для нескольких тарифов одним запросом (вместо запроса на каждый тариф)."""
    if not tariff_ids:
        return {}
    rows = (
        await session.execute(
            select(TariffTier.tariff_id, TariffTier.threshold, TariffTier.rate)
            .where(TariffTier.tariff_id.in_(tariff_ids))
            .order_by(TariffTier.tariff_id, TariffTier.threshold)
        )
    ).all()
    result: dict[int, list[tuple[Decimal, Decimal]]] = {tid: [] for tid in tariff_ids}
    for tariff_id, threshold, rate in rows:
        result[tariff_id].append((threshold, rate))
    return result


def split_by_tiers(
    prior: Decimal, quantity: Decimal, base_rate: Decimal, tiers: list[tuple[Decimal, Decimal]]
) -> list[tuple[Decimal, Decimal]]:
    """Делит количество по ступеням с учётом уже выполненного объёма: [(кол-во, ставка)].

    Пример: до 50 — 400, с 51-го — 450; уже сделано 45, новая операция 10 → [(5, 400), (5, 450)].
    """
    bands = [(Decimal(0), base_rate), *tiers]
    start, end = prior, prior + quantity
    pieces = []
    for index, (low, rate) in enumerate(bands):
        high = bands[index + 1][0] if index + 1 < len(bands) else None
        lo = max(start, low)
        hi = end if high is None else min(end, high)
        if hi > lo:
            pieces.append((hi - lo, rate))
    return pieces


# ------------------------------------------------------------------ коэффициенты


async def create_coefficient(session: AsyncSession, actor, code: str, factor: Decimal, name: str) -> Coefficient:
    actor.require(Perm.TARIFF_MANAGE)
    code = code.strip().lower().lstrip("*")
    if not code or " " in code or len(code) > 20:
        raise ValidationError("Код коэффициента - одно слово до 20 символов")
    if factor <= 0 or scale_of(factor) > 4:
        raise ValidationError("Коэффициент должен быть положительным числом, не больше 4 знаков после запятой")
    if await session.get(Coefficient, code) is not None:
        raise ValidationError(f"Коэффициент {code} уже есть. Значение не меняется, чтобы не исказить историю")
    coef = Coefficient(code=code, name=name.strip() or code, factor=factor, created_by=actor.user.id)
    session.add(coef)
    await audit(session, actor.user.id, "tariff.coefficient_create", "coefficient", code, {"factor": factor})
    return coef


async def set_coefficient_active(session: AsyncSession, actor, code: str, active: bool) -> Coefficient:
    actor.require(Perm.TARIFF_MANAGE)
    coef = await session.get(Coefficient, code.strip().lower().lstrip("*"))
    if coef is None:
        raise NotFound(f"Нет коэффициента {code}")
    coef.is_active = active
    await audit(session, actor.user.id, "tariff.coefficient_active", "coefficient", coef.code, {"active": active})
    return coef


async def list_coefficients(session: AsyncSession) -> list[Coefficient]:
    return list((await session.execute(select(Coefficient).order_by(Coefficient.code))).scalars())


async def resolve_coefficients(session: AsyncSession, codes: list[str] | None) -> tuple[Decimal, list[str]]:
    """Перемножает коэффициенты: «срочно» ×1,5 и «выходной» ×2 дают ×3."""
    factor = Decimal(1)
    used: list[str] = []
    for raw in codes or []:
        code = raw.strip().lower().lstrip("*")
        if not code or code in used:
            continue
        coef = await session.get(Coefficient, code)
        if coef is None or not coef.is_active:
            raise ValidationError(f"Нет действующего коэффициента {code}")
        factor *= coef.factor
        used.append(code)
    return factor, used


async def current_rates(
    session: AsyncSession, work_type_id: int, today: dt.date
) -> list[tuple[Tariff, str | None, str | None, list[tuple[Decimal, Decimal]], bool]]:
    """Действующая ставка по каждой области (общая / подразделение / сотрудник) и будущие ставки.

    Возвращает (тариф, подразделение, сотрудник, ступени, будущая_ли).
    """
    rows = await tariff_history(session, work_type_id)
    tiers_by_tariff = await tiers_of_many(session, [t.id for t, _, _ in rows])
    seen: set[tuple] = set()
    result = []
    for tariff, dept_name, emp_name in rows:
        scope = (tariff.department_id, tariff.employee_id)
        if tariff.valid_from > today:
            result.append((tariff, dept_name, emp_name, tiers_by_tariff[tariff.id], True))
            continue
        if scope in seen:
            continue
        seen.add(scope)
        result.append((tariff, dept_name, emp_name, tiers_by_tariff[tariff.id], False))
    return result
