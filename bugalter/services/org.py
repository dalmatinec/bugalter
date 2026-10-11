"""Подразделения, сотрудники, привязка рабочих групп."""

import re

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Department, Employee, EmployeeDepartment, TelegramChat, User
from ..domain.errors import NotFound, ValidationError
from ..domain.permissions import Perm
from .audit import audit

_CODE_RE = re.compile(r"^[\w\-]{1,32}$", re.UNICODE)


def _check_code(code: str, what: str) -> str:
    code = code.strip().lower()
    if not _CODE_RE.match(code):
        raise ValidationError(f"Код {what} — одно слово до 32 символов (буквы, цифры, -, _): «{code}»")
    return code


async def create_department(session: AsyncSession, actor, code: str, name: str) -> Department:
    actor.require(Perm.ORG_MANAGE)
    code = _check_code(code, "подразделения")
    if await get_department(session, code, required=False):
        raise ValidationError(f"Подразделение {code} уже есть")
    dept = Department(code=code, name=name.strip() or code)
    session.add(dept)
    await session.flush()
    await audit(session, actor.user.id, "org.department_create", "department", dept.id, {"code": code, "name": name})
    return dept


async def get_department(session: AsyncSession, code: str, required: bool = True) -> Department | None:
    dept = (
        await session.execute(select(Department).where(Department.code == code.strip().lower().lstrip("@")))
    ).scalar_one_or_none()
    if dept is None and required:
        raise NotFound(f"Нет подразделения «{code}». Список: /depts")
    return dept


async def list_departments(session: AsyncSession) -> list[Department]:
    return list((await session.execute(select(Department).order_by(Department.code))).scalars())


async def create_employee(
    session: AsyncSession, actor, code: str, full_name: str, position: str | None = None
) -> Employee:
    # Руководитель с правом org.manage хотя бы в одном подразделении может завести сотрудника,
    # а затем прикрепить его к своему подразделению.
    if not actor.can_anywhere(Perm.ORG_MANAGE):
        actor.require(Perm.ORG_MANAGE)
    code = _check_code(code, "сотрудника")
    if await get_employee(session, code, required=False):
        raise ValidationError(f"Сотрудник с кодом {code} уже есть")
    if not full_name.strip():
        raise ValidationError("Укажите ФИО сотрудника")
    employee = Employee(code=code, full_name=full_name.strip(), position=position)
    session.add(employee)
    await session.flush()
    await audit(
        session, actor.user.id, "org.employee_create", "employee", employee.id, {"code": code, "name": full_name}
    )
    return employee


async def get_employee(session: AsyncSession, code: str, required: bool = True) -> Employee | None:
    employee = (
        await session.execute(select(Employee).where(Employee.code == code.strip().lower().lstrip("@")))
    ).scalar_one_or_none()
    if employee is None and required:
        raise NotFound(f"Нет сотрудника с кодом «{code}». Список: /emps")
    return employee


async def link_user(session: AsyncSession, actor, employee: Employee, user: User) -> None:
    await _require_org_for_employee(session, actor, employee)
    other = (
        await session.execute(select(Employee).where(Employee.user_id == user.id, Employee.id != employee.id))
    ).scalar_one_or_none()
    if other:
        raise ValidationError(f"Этот Telegram уже привязан к сотруднику {other.code}")
    employee.user_id = user.id
    await audit(session, actor.user.id, "org.employee_link", "employee", employee.id, {"user_id": user.id})


async def attach_to_department(session: AsyncSession, actor, employee: Employee, dept: Department) -> None:
    actor.require(Perm.ORG_MANAGE, dept.id)
    stmt = insert(EmployeeDepartment).values(employee_id=employee.id, department_id=dept.id, is_active=True)
    stmt = stmt.on_conflict_do_update(
        index_elements=[EmployeeDepartment.employee_id, EmployeeDepartment.department_id],
        set_={"is_active": True},
    )
    await session.execute(stmt)
    await audit(session, actor.user.id, "org.employee_attach", "employee", employee.id, {"department_id": dept.id})


async def detach_from_department(session: AsyncSession, actor, employee: Employee, dept: Department) -> None:
    actor.require(Perm.ORG_MANAGE, dept.id)
    link = (
        await session.execute(
            select(EmployeeDepartment).where(
                EmployeeDepartment.employee_id == employee.id, EmployeeDepartment.department_id == dept.id
            )
        )
    ).scalar_one_or_none()
    if link is None or not link.is_active:
        raise NotFound(f"{employee.code} не состоит в подразделении {dept.code}")
    link.is_active = False
    await audit(session, actor.user.id, "org.employee_detach", "employee", employee.id, {"department_id": dept.id})


async def set_employee_active(session: AsyncSession, actor, employee: Employee, active: bool) -> None:
    await _require_org_for_employee(session, actor, employee)
    employee.is_active = active
    employee.dismissed_at = None if active else func.now()
    await audit(session, actor.user.id, "org.employee_active", "employee", employee.id, {"active": active})


async def _require_org_for_employee(session: AsyncSession, actor, employee: Employee) -> None:
    if actor.can(Perm.ORG_MANAGE):
        return
    for dept_id in await employee_department_ids(session, employee.id):
        if actor.can(Perm.ORG_MANAGE, dept_id):
            return
    actor.require(Perm.ORG_MANAGE)


async def employee_department_ids(session: AsyncSession, employee_id: int) -> list[int]:
    return list(
        (
            await session.execute(
                select(EmployeeDepartment.department_id).where(
                    EmployeeDepartment.employee_id == employee_id, EmployeeDepartment.is_active
                )
            )
        ).scalars()
    )


async def is_member(session: AsyncSession, employee_id: int, department_id: int) -> bool:
    return department_id in await employee_department_ids(session, employee_id)


async def department_employees(session: AsyncSession, department_id: int) -> list[Employee]:
    return list(
        (
            await session.execute(
                select(Employee)
                .join(EmployeeDepartment, EmployeeDepartment.employee_id == Employee.id)
                .where(
                    EmployeeDepartment.department_id == department_id,
                    EmployeeDepartment.is_active,
                    Employee.is_active,
                )
                .order_by(Employee.full_name)
            )
        ).scalars()
    )


async def list_employees(session: AsyncSession) -> list[tuple[Employee, list[str]]]:
    employees = list((await session.execute(select(Employee).order_by(Employee.code))).scalars())
    links = (
        await session.execute(
            select(EmployeeDepartment.employee_id, Department.code)
            .join(Department, Department.id == EmployeeDepartment.department_id)
            .where(EmployeeDepartment.is_active)
        )
    ).all()
    by_emp: dict[int, list[str]] = {}
    for emp_id, dept_code in links:
        by_emp.setdefault(emp_id, []).append(dept_code)
    return [(e, sorted(by_emp.get(e.id, []))) for e in employees]


async def default_department_id(session: AsyncSession, employee_id: int) -> int | None:
    """Подразделение сотрудника, если оно одно; иначе None."""
    ids = await employee_department_ids(session, employee_id)
    return ids[0] if len(ids) == 1 else None


async def bind_chat(session: AsyncSession, actor, chat_id: int, title: str, dept: Department) -> None:
    actor.require(Perm.ORG_MANAGE, dept.id)
    stmt = insert(TelegramChat).values(
        chat_id=chat_id, title=title or "", department_id=dept.id, bound_by=actor.user.id, is_active=True
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[TelegramChat.chat_id],
        set_={"department_id": dept.id, "title": title or "", "bound_by": actor.user.id, "is_active": True},
    )
    await session.execute(stmt)
    await audit(session, actor.user.id, "org.chat_bind", "chat", chat_id, {"department_id": dept.id}, chat_id)


async def unbind_chat(session: AsyncSession, actor, chat_id: int) -> None:
    chat = await session.get(TelegramChat, chat_id)
    if chat is None or not chat.is_active:
        raise NotFound("Группа не привязана к подразделению")
    actor.require(Perm.ORG_MANAGE, chat.department_id)
    chat.is_active = False
    await audit(session, actor.user.id, "org.chat_unbind", "chat", chat_id, None, chat_id)


async def chat_department(session: AsyncSession, chat_id: int) -> Department | None:
    return (
        await session.execute(
            select(Department)
            .join(TelegramChat, TelegramChat.department_id == Department.id)
            .where(TelegramChat.chat_id == chat_id, TelegramChat.is_active)
        )
    ).scalar_one_or_none()


async def department_chats(session: AsyncSession, department_id: int) -> list[int]:
    return list(
        (
            await session.execute(
                select(TelegramChat.chat_id).where(TelegramChat.department_id == department_id, TelegramChat.is_active)
            )
        ).scalars()
    )
