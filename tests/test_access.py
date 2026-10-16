import pytest

from bugalter.domain.errors import AccessDenied, ValidationError
from bugalter.domain.permissions import Perm
from bugalter.services import access, org
from bugalter.services import settings as settings_service


async def test_department_scoping(world):
    w = world
    assert w.manager.can(Perm.WORK_CONFIRM, w.north.id)
    assert not w.manager.can(Perm.WORK_CONFIRM, w.south.id)
    assert not w.manager.can(Perm.WORK_CONFIRM)  # глобального права нет
    assert w.admin.can(Perm.WORK_CONFIRM, w.south.id)
    assert not w.admin.can(Perm.PERIOD_REOPEN)
    assert w.owner.can(Perm.PERIOD_REOPEN) and w.owner.is_owner
    assert not w.ivan_actor.can_anywhere(Perm.WORK_CREATE)


async def test_operator_in_several_departments(world, session):
    w = world
    await access.grant_role(session, w.admin, w.operator.user, "operator", w.south)
    operator = await w.reload(w.operator)
    assert operator.can(Perm.WORK_CREATE, w.north.id) and operator.can(Perm.WORK_CREATE, w.south.id)
    assert operator.departments_with(Perm.WORK_CREATE) == {w.north.id, w.south.id}


async def test_no_privilege_escalation(world, session):
    w = world
    # руководитель «Севера» без права выдачи ролей
    with pytest.raises(AccessDenied):
        await access.grant_role(session, w.manager, w.ivan_actor.user, "operator", w.north)
    # администратор не может выдать роль владельца и создать роль
    with pytest.raises(AccessDenied):
        await access.grant_role(session, w.admin, w.ivan_actor.user, "owner", None)
    with pytest.raises(AccessDenied):
        await access.create_role(session, w.admin, "x", "X", ["work.create"])
    # владелец не может вложить права владельца в обычную роль
    with pytest.raises(ValidationError):
        await access.create_role(session, w.owner, "x", "X", ["period.reopen"])
    role = await access.create_role(session, w.owner, "courier_lead", "Старший курьер", ["work.create", "work.view"])
    grant = await access.grant_role(session, w.admin, w.ivan_actor.user, role.code, w.north)
    ivan = await w.reload(w.ivan_actor)
    assert ivan.can(Perm.WORK_CREATE, w.north.id)
    await access.revoke_grant(session, w.admin, grant.id)
    assert not (await w.reload(ivan)).can(Perm.WORK_CREATE, w.north.id)


async def test_cannot_revoke_last_owner(world, session):
    w = world
    owner_grant = next(g for g in w.owner.grants if g.role_code == "owner")
    with pytest.raises(ValidationError):
        await access.revoke_grant(session, w.owner, owner_grant.id)


async def test_users_with_perm_for_notifications(world, session):
    w = world
    users = {u.telegram_id for u in await access.users_with_perm(session, Perm.STOCK_RECEIPT_CONFIRM, w.north.id)}
    assert users == {1, 20, 30, 50}  # владелец, руководитель, оператор, админ
    south = {u.telegram_id for u in await access.users_with_perm(session, Perm.STOCK_RECEIPT_CONFIRM, w.south.id)}
    assert south == {1, 50}


async def test_settings_change_is_audited_and_validated(world, session):
    w = world
    with pytest.raises(AccessDenied):
        await settings_service.set_setting(session, w.manager, "currency", "RUB")
    with pytest.raises(ValidationError):
        await settings_service.set_setting(session, w.owner, "currency", "XXX")
    await settings_service.set_setting(session, w.owner, "currency", "rub")
    await settings_service.set_setting(session, w.owner, "group_show_amounts", "да")
    current = await settings_service.get_settings(session)
    assert current.currency == "RUB" and current.group_show_amounts is True


async def test_employee_membership_and_chat_binding(world, session):
    w = world
    await org.bind_chat(session, w.owner, -100500, "Север — работа", w.north)
    assert (await org.chat_department(session, -100500)).id == w.north.id
    with pytest.raises(AccessDenied):
        await org.bind_chat(session, w.manager, -100600, "Юг", w.south)
    await org.detach_from_department(session, w.owner, w.ivan, w.south)
    assert await org.employee_department_ids(session, w.ivan.id) == [w.north.id]


async def test_delete_employee(world, session, settings):
    import datetime as dt
    from decimal import Decimal

    from bugalter.db.models import Employee
    from bugalter.services import tariffs, work

    w = world
    # Без истории — стирается полностью
    temp = await org.create_employee_by_title(session, w.owner, "Стажёр")
    await org.attach_to_department(session, w.owner, temp, w.north)
    with pytest.raises(ValidationError, match="уволите"):
        await org.delete_employee(session, w.owner, temp)
    await org.set_employee_active(session, w.owner, temp, False)
    with pytest.raises(AccessDenied):
        await org.delete_employee(session, w.admin, temp)
    assert await org.delete_employee(session, w.owner, temp) is True
    assert await session.get(Employee, temp.id) is None

    # С историей — пропадает из списков, Telegram отвязан, суммы остаются
    await tariffs.create_tariffs(
        session,
        w.owner,
        settings,
        work_type=w.delivery,
        rate=Decimal(100),
        currency="USD",
        department=w.north,
        employees=None,
        valid_from=dt.date(2026, 10, 1),
    )
    entry = (
        await work.create_entry(
            session,
            w.operator,
            settings,
            idempotency_key="del-1",
            department_id=w.north.id,
            employee_id=w.ivan.id,
            work_type_id=w.delivery.id,
            quantity=Decimal(1),
        )
    ).entry
    await work.confirm_entry(session, w.owner, settings, entry.id)
    await org.set_employee_active(session, w.owner, w.ivan, False)
    assert await org.delete_employee(session, w.owner, w.ivan) is False
    assert w.ivan.user_id is None and w.ivan.deleted_at is not None
    assert w.ivan.id not in {e.id for e, _ in await org.list_employees(session)}
    ivan = await w.reload(w.ivan_actor)
    assert ivan.employee is None and not ivan.grants


async def test_sync_owners_from_config(session):
    await access.ensure_owner(session, 1)
    await access.ensure_owner(session, 21)
    await access.ensure_owner(session, 22)
    assert await access.sync_owners(session, [1, 22]) == 1
    assert await access.sync_owners(session, []) == 0  # пустой список ничего не снимает
    owners = {u.telegram_id for u in await access.owners(session)}
    assert owners == {1, 22}
