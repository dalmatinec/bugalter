"""Подразделения, сотрудники, привязка рабочих групп."""

import datetime as dt
import re
import secrets

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Department, Employee, EmployeeDepartment, EmployeeInvite, TelegramChat, User
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
    if (await session.execute(select(Department.id).where(Department.code == code))).first():
        raise ValidationError(f"Подразделение {code} уже есть")
    dept = Department(code=code, name=name.strip() or code)
    session.add(dept)
    await session.flush()
    await audit(session, actor.user.id, "org.department_create", "department", dept.id, {"code": code, "name": name})
    return dept


async def lookup(session: AsyncSession, model, code_col, name_col, text: str):
    """Ищет по коду, затем по названию целиком, затем по началу названия — если совпадение однозначное.

    Чтобы в командах можно было писать «доставка», «иван» или «Иван Иванов», а не только служебный код.
    """
    raw = (text or "").strip().lstrip("@")
    if not raw:
        return None
    key = raw.lower()
    found = (await session.execute(select(model).where(code_col == key))).scalar_one_or_none()
    if found is not None:
        return found
    for condition in (func.lower(name_col) == key, func.lower(name_col).startswith(key)):
        rows = (await session.execute(select(model).where(condition).limit(2))).scalars().all()
        if len(rows) == 1:
            return rows[0]
    return None


async def get_department(session: AsyncSession, code: str, required: bool = True) -> Department | None:
    dept = await lookup(session, Department, Department.code, Department.name, code)
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
    if (await session.execute(select(Employee.id).where(Employee.code == code))).first():
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
    employee = await lookup(session, Employee, Employee.code, Employee.full_name, code)
    if employee is None and required:
        raise NotFound(f"Не нашёл сотрудника «{code}». Список: /emps")
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


async def bind_chat(
    session: AsyncSession, actor, chat_id: int, title: str, dept: Department, employee: Employee | None = None
) -> None:
    """Привязывает группу к подразделению. С employee — это личная группа сотрудника:
    в ней мастер ввода сразу подставляет этого сотрудника."""
    actor.require(Perm.ORG_MANAGE, dept.id)
    if employee is not None and not await is_member(session, employee.id, dept.id):
        raise ValidationError(f"{employee.full_name} не состоит в подразделении {dept.name}")
    values = {
        "department_id": dept.id,
        "employee_id": employee.id if employee else None,
        "title": title or "",
        "bound_by": actor.user.id,
        "is_active": True,
    }
    stmt = insert(TelegramChat).values(chat_id=chat_id, **values)
    stmt = stmt.on_conflict_do_update(index_elements=[TelegramChat.chat_id], set_=values)
    await session.execute(stmt)
    await audit(
        session,
        actor.user.id,
        "org.chat_bind",
        "chat",
        chat_id,
        {"department_id": dept.id, "employee_id": employee.id if employee else None},
        chat_id,
    )


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


async def chat_binding(session: AsyncSession, chat_id: int) -> tuple[Department | None, Employee | None]:
    row = (
        await session.execute(
            select(Department, TelegramChat.employee_id)
            .join(TelegramChat, TelegramChat.department_id == Department.id)
            .where(TelegramChat.chat_id == chat_id, TelegramChat.is_active)
        )
    ).first()
    if row is None:
        return None, None
    dept, employee_id = row
    employee = await session.get(Employee, employee_id) if employee_id else None
    return dept, employee


async def list_chats(session: AsyncSession) -> list[tuple[TelegramChat, Department, Employee | None]]:
    rows = (
        await session.execute(
            select(TelegramChat, Department, Employee)
            .join(Department, Department.id == TelegramChat.department_id)
            .outerjoin(Employee, Employee.id == TelegramChat.employee_id)
            .where(TelegramChat.is_active)
            .order_by(Department.name, Employee.full_name.nulls_first())
        )
    ).all()
    return [tuple(r) for r in rows]


async def department_chats(session: AsyncSession, department_id: int) -> list[int]:
    return list(
        (
            await session.execute(
                select(TelegramChat.chat_id).where(TelegramChat.department_id == department_id, TelegramChat.is_active)
            )
        ).scalars()
    )


# ------------------------------------------------------------------ автокоды и приглашения

_TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        [
            "a",
            "b",
            "v",
            "g",
            "d",
            "e",
            "e",
            "zh",
            "z",
            "i",
            "y",
            "k",
            "l",
            "m",
            "n",
            "o",
            "p",
            "r",
            "s",
            "t",
            "u",
            "f",
            "h",
            "ts",
            "ch",
            "sh",
            "sch",
            "",
            "y",
            "",
            "e",
            "yu",
            "ya",
        ],
        strict=True,
    )
)


def slugify(text: str, max_len: int = 24) -> str:
    """«Иван Иванов» → «ivan», «Север-2» → «sever-2». Для кодов, которые удобно набирать в командах."""
    word = (text or "").strip().lower().split()[0] if (text or "").strip() else ""
    out = "".join(_TRANSLIT.get(ch, ch) for ch in word)
    out = re.sub(r"[^a-z0-9\-_]", "", out)
    return out[:max_len] or "x"


async def unique_code(session: AsyncSession, column, text: str) -> str:
    """Свободный код на основе названия: ivan, ivan2, ivan3… column — Employee.code, Product.sku и т. п."""
    base = slugify(text)
    code, n = base, 1
    while (await session.execute(select(column).where(column == code))).first() is not None:
        n += 1
        code = f"{base}{n}"
    return code


DEFAULT_DEPARTMENT = ("main", "Основное")


async def ensure_default_department(session: AsyncSession) -> None:
    """Одна точка создаётся сама: пока филиал один, бот о нём не спрашивает."""
    if (await session.execute(select(Department.id).limit(1))).first() is None:
        code, name = DEFAULT_DEPARTMENT
        session.add(Department(code=code, name=name))
        await audit(session, None, "org.department_create", "department", code, {"default": True})


async def active_departments(session: AsyncSession) -> list[Department]:
    return list(
        (await session.execute(select(Department).where(Department.is_active).order_by(Department.id))).scalars()
    )


async def single_department(session: AsyncSession) -> Department | None:
    """Если точка одна — она; иначе None (надо выбирать)."""
    depts = await active_departments(session)
    return depts[0] if len(depts) == 1 else None


async def create_invite(
    session: AsyncSession, actor, employee: Employee, days: int = 7, role_code: str | None = None
) -> str:
    await _require_org_for_employee(session, actor, employee)
    token = secrets.token_urlsafe(18)
    session.add(
        EmployeeInvite(
            token=token,
            employee_id=employee.id,
            role_code=role_code,
            created_by=actor.user.id,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=days),
        )
    )
    await audit(session, actor.user.id, "org.invite_create", "employee", employee.id)
    return token


async def accept_invite(session: AsyncSession, user: User, token: str) -> tuple[Employee, EmployeeInvite]:
    """Сотрудник открыл ссылку-приглашение: привязываем его Telegram к карточке."""
    invite = await session.get(EmployeeInvite, token, with_for_update=True)
    if invite is None or invite.used_at is not None or invite.expires_at < dt.datetime.now(dt.UTC):
        raise ValidationError("Ссылка-приглашение недействительна или уже использована. Попросите новую.")
    employee = await session.get(Employee, invite.employee_id)
    if not employee.is_active:
        raise ValidationError("Карточка сотрудника неактивна")
    other = (
        await session.execute(select(Employee).where(Employee.user_id == user.id, Employee.id != employee.id))
    ).scalar_one_or_none()
    if other:
        raise ValidationError(f"Ваш Telegram уже привязан к сотруднику {other.full_name}")
    if employee.user_id and employee.user_id != user.id:
        raise ValidationError("К этой карточке уже привязан другой Telegram. Обратитесь к администратору.")
    employee.user_id = user.id
    invite.used_at = func.now()
    invite.used_by = user.id
    await audit(session, user.id, "org.invite_accept", "employee", employee.id)
    return employee, invite
