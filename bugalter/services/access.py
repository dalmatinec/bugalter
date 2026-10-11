"""Пользователи, роли и проверка прав."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AccessGrant, Department, Employee, Role, User
from ..domain.errors import AccessDenied, NotFound, ValidationError
from ..domain.permissions import ALL_PERMS, OWNER_ONLY_PERMS, OWNER_ROLE, PERM_TITLES, Perm
from .audit import audit


@dataclass(frozen=True)
class GrantView:
    id: int
    role_code: str
    department_id: int | None
    permissions: frozenset[str]


@dataclass
class Actor:
    """Кто выполняет действие и что ему разрешено."""

    user: User
    employee: Employee | None
    grants: list[GrantView] = field(default_factory=list)

    @property
    def is_owner(self) -> bool:
        return any(g.role_code == OWNER_ROLE and g.department_id is None for g in self.grants)

    def can(self, perm: str, department_id: int | None = None) -> bool:
        """department_id=None — нужно право во всех подразделениях (глобальная выдача)."""
        for grant in self.grants:
            if perm not in grant.permissions:
                continue
            if grant.department_id is None or grant.department_id == department_id:
                return True
        return False

    def can_anywhere(self, perm: str) -> bool:
        return any(perm in g.permissions for g in self.grants)

    def departments_with(self, perm: str) -> set[int] | None:
        """None — во всех подразделениях; иначе множество id."""
        result: set[int] = set()
        for grant in self.grants:
            if perm in grant.permissions:
                if grant.department_id is None:
                    return None
                result.add(grant.department_id)
        return result

    def require(self, perm: str, department_id: int | None = None) -> None:
        if not self.can(perm, department_id):
            title = PERM_TITLES.get(perm, perm)
            raise AccessDenied(f"Недостаточно прав: {title}")

    def is_self(self, employee_id: int) -> bool:
        return self.employee is not None and self.employee.id == employee_id


async def upsert_user(session: AsyncSession, telegram_id: int, username: str | None, full_name: str) -> User:
    """Пользователь по Telegram ID. Пишет в БД только если пользователь новый или сменил имя."""
    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    full_name = full_name or ""
    if user is not None:
        if user.username != username or (full_name and user.full_name != full_name):
            user.username = username
            user.full_name = full_name or user.full_name
        return user
    stmt = insert(User).values(telegram_id=telegram_id, username=username, full_name=full_name)
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.telegram_id], set_={"username": username, "full_name": full_name}
    ).returning(User.id)
    user_id = (await session.execute(stmt)).scalar_one()
    return await session.get(User, user_id, populate_existing=True)


async def load_grants(session: AsyncSession, user_id: int) -> list[GrantView]:
    rows = (
        await session.execute(
            select(AccessGrant.id, AccessGrant.role_code, AccessGrant.department_id, Role.permissions)
            .join(Role, Role.code == AccessGrant.role_code)
            .where(AccessGrant.user_id == user_id, AccessGrant.revoked_at.is_(None))
        )
    ).all()
    return [GrantView(r.id, r.role_code, r.department_id, frozenset(r.permissions)) for r in rows]


async def load_actor(
    session: AsyncSession, telegram_id: int, username: str | None = None, full_name: str = ""
) -> Actor:
    user = await upsert_user(session, telegram_id, username, full_name)
    employee = (
        await session.execute(select(Employee).where(Employee.user_id == user.id, Employee.is_active))
    ).scalar_one_or_none()
    return Actor(user=user, employee=employee, grants=await load_grants(session, user.id))


async def actor_for_user(session: AsyncSession, user: User) -> Actor:
    employee = (
        await session.execute(select(Employee).where(Employee.user_id == user.id, Employee.is_active))
    ).scalar_one_or_none()
    return Actor(user=user, employee=employee, grants=await load_grants(session, user.id))


async def find_user(session: AsyncSession, ref: str) -> User:
    """Пользователь по Telegram ID или @username (username — только если человек уже писал боту)."""
    ref = ref.strip()
    if ref.lstrip("-").isdigit():
        user = (await session.execute(select(User).where(User.telegram_id == int(ref)))).scalar_one_or_none()
        if user is None:
            user = User(telegram_id=int(ref), full_name="")
            session.add(user)
            await session.flush()
        return user
    username = ref.lstrip("@")
    user = (
        await session.execute(select(User).where(func.lower(User.username) == username.lower()))
    ).scalar_one_or_none()
    if user is None:
        raise NotFound(f"Пользователь @{username} ещё не писал боту. Попросите его нажать /start или укажите ID")
    return user


async def ensure_owner(session: AsyncSession, telegram_id: int) -> None:
    """Идемпотентно выдаёт роль owner пользователю из OWNER_TELEGRAM_IDS."""
    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id, full_name="")
        session.add(user)
        await session.flush()
    exists = (
        await session.execute(
            select(AccessGrant.id).where(
                AccessGrant.user_id == user.id,
                AccessGrant.role_code == OWNER_ROLE,
                AccessGrant.department_id.is_(None),
                AccessGrant.revoked_at.is_(None),
            )
        )
    ).first()
    if not exists:
        session.add(AccessGrant(user_id=user.id, role_code=OWNER_ROLE, department_id=None))
        await audit(session, None, "access.bootstrap_owner", "user", user.id, {"telegram_id": telegram_id})


async def grant_role(
    session: AsyncSession, actor: Actor, user: User, role_code: str, department: Department | None
) -> AccessGrant:
    department_id = department.id if department else None
    actor.require(Perm.ACCESS_MANAGE, department_id)
    role = await session.get(Role, role_code)
    if role is None:
        raise NotFound(f"Нет роли {role_code}. Список: /roles")
    if (role.code == OWNER_ROLE or OWNER_ONLY_PERMS & set(role.permissions)) and not actor.is_owner:
        raise AccessDenied("Эту роль может выдать только владелец")
    if not actor.is_owner:
        # Нельзя выдать больше прав, чем есть у самого выдающего в этом подразделении.
        missing = [p for p in role.permissions if not actor.can(p, department_id)]
        if missing:
            raise AccessDenied("Нельзя выдать права, которых нет у вас: " + ", ".join(sorted(missing)))
    if role.code == OWNER_ROLE and department_id is not None:
        raise ValidationError("Роль владельца выдаётся только на все подразделения")
    existing = (
        await session.execute(
            select(AccessGrant).where(
                AccessGrant.user_id == user.id,
                AccessGrant.role_code == role.code,
                AccessGrant.department_id.is_(None)
                if department_id is None
                else AccessGrant.department_id == department_id,
                AccessGrant.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    grant = AccessGrant(user_id=user.id, role_code=role.code, department_id=department_id, granted_by=actor.user.id)
    session.add(grant)
    await session.flush()
    await audit(
        session,
        actor.user.id,
        "access.grant",
        "access_grant",
        grant.id,
        {"user_id": user.id, "role": role.code, "department_id": department_id},
    )
    return grant


async def revoke_grant(session: AsyncSession, actor: Actor, grant_id: int) -> AccessGrant:
    grant = await session.get(AccessGrant, grant_id, with_for_update=True)
    if grant is None or grant.revoked_at is not None:
        raise NotFound(f"Активная выдача #{grant_id} не найдена")
    actor.require(Perm.ACCESS_MANAGE, grant.department_id)
    role = await session.get(Role, grant.role_code)
    if (grant.role_code == OWNER_ROLE or OWNER_ONLY_PERMS & set(role.permissions)) and not actor.is_owner:
        raise AccessDenied("Эту роль может отозвать только владелец")
    if grant.role_code == OWNER_ROLE:
        owners = (
            await session.execute(
                select(func.count())
                .select_from(AccessGrant)
                .where(AccessGrant.role_code == OWNER_ROLE, AccessGrant.revoked_at.is_(None))
            )
        ).scalar_one()
        if owners <= 1:
            raise ValidationError("Нельзя отозвать роль у последнего владельца")
    grant.revoked_at = func.now()
    grant.revoked_by = actor.user.id
    await audit(session, actor.user.id, "access.revoke", "access_grant", grant.id, {"role": grant.role_code})
    return grant


async def create_role(session: AsyncSession, actor: Actor, code: str, name: str, perms: list[str]) -> Role:
    actor.require(Perm.ROLE_MANAGE)
    code = code.strip().lower()
    unknown = [p for p in perms if p not in ALL_PERMS]
    if unknown:
        raise ValidationError("Неизвестные права: " + ", ".join(unknown))
    if OWNER_ONLY_PERMS & set(perms):
        raise ValidationError("Права владельца нельзя включать в другие роли")
    role = await session.get(Role, code)
    if role is not None and role.is_system:
        raise ValidationError("Системные роли не редактируются — создайте новую")
    if role is None:
        role = Role(code=code, name=name, permissions=sorted(set(perms)), is_system=False)
        session.add(role)
    else:
        role.name = name
        role.permissions = sorted(set(perms))
    await session.flush()
    await audit(session, actor.user.id, "access.role_save", "role", code, {"name": name, "permissions": perms})
    return role


async def users_with_perm(session: AsyncSession, perm: str, department_id: int | None) -> list[User]:
    """Кого уведомлять: все, у кого есть право в этом подразделении (или глобально)."""
    conditions = [AccessGrant.revoked_at.is_(None), Role.permissions.any(perm), User.is_active]
    if department_id is None:
        conditions.append(AccessGrant.department_id.is_(None))
    else:
        conditions.append((AccessGrant.department_id.is_(None)) | (AccessGrant.department_id == department_id))
    rows = (
        await session.execute(
            select(User)
            .join(AccessGrant, AccessGrant.user_id == User.id)
            .join(Role, Role.code == AccessGrant.role_code)
            .where(*conditions)
            .distinct()
        )
    ).scalars()
    return list(rows)


async def list_grants(session: AsyncSession, user_id: int | None = None) -> list[tuple]:
    query = (
        select(AccessGrant, User, Department)
        .join(User, User.id == AccessGrant.user_id)
        .outerjoin(Department, Department.id == AccessGrant.department_id)
        .where(AccessGrant.revoked_at.is_(None))
        .order_by(AccessGrant.id)
    )
    if user_id is not None:
        query = query.where(AccessGrant.user_id == user_id)
    return list((await session.execute(query)).all())
