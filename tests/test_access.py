import pytest

from bugalter.domain.errors import AccessDenied, ValidationError
from bugalter.domain.permissions import Perm
from bugalter.services import access, org
from bugalter.services import settings as settings_service


async def test_department_scoping(world):
    w = world
    assert w.manager.can(Perm.WORK_CREATE, w.north.id)
    assert not w.manager.can(Perm.WORK_CREATE, w.south.id)
    assert not w.manager.can(Perm.WORK_CREATE)  # глобального права нет
    # Подтверждение операций и выплат — только владелец, даже у админа с остальными полными правами.
    assert not w.manager.can(Perm.WORK_CONFIRM, w.north.id)
    assert not w.admin.can(Perm.WORK_CONFIRM, w.south.id)
    assert not w.admin.can(Perm.PAYROLL_PAYOUT_CONFIRM, w.south.id)
    assert w.owner.can(Perm.WORK_CONFIRM, w.south.id) and w.owner.can(Perm.PAYROLL_PAYOUT_CONFIRM, w.south.id)
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
