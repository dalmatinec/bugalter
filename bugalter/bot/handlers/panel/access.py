"""Доступ: кто какую роль имеет, выдача и отзыв ролей."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy import select

from ....db.models import Department, Employee, User
from ....domain.permissions import OWNER_ROLE, Perm
from ....services import access, org
from ...ui import h
from .base import ROLES_HELP, ask, cancel_kb, form, kb, role_choice, role_names, show
from .employees import employee_view

router = Router(name="panel.access")


async def access_view(session):
    grants = await access.list_grants(session)
    names = await role_names(session)
    lines = ["🔑 <b>Роли и доступ</b>", ""]
    rows = []
    for g, user, dept in grants:
        emp = (await session.execute(select(Employee).where(Employee.user_id == user.id))).scalar_one_or_none()
        who = (
            emp.full_name
            if emp
            else (user.full_name or f"@{user.username}" if user.username else str(user.telegram_id))
        )
        scope = dept.name if dept else "все филиалы"
        role_name = names.get(g.role_code, g.role_code)
        lines.append(f"• {h(who)}: <b>{h(role_name)}</b> ({h(scope)})")
        if g.role_code != OWNER_ROLE:
            rows.append([(f"❌ {who}: {role_name}", f"p:a:rv:{g.id}")])
    rows.append([("➕ Выдать роль по ID", "p:a:id")])
    rows.append([("📖 Про роли", "p:a:help")])
    rows.append([("⬅️ Назад", "p:more")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "p:a")
async def access_section(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    show(callback, notifier, *await access_view(session))


@router.callback_query(F.data == "p:a:help")
async def access_help(callback: CallbackQuery, notifier):
    show(callback, notifier, "📖 <b>Роли</b>\n\n" + ROLES_HELP, kb([[("⬅️ Назад", "p:a")]]))


@router.callback_query(F.data.startswith("p:e:rl:") | F.data.startswith("p:a:rl:"))
async def role_scope(callback: CallbackQuery, session, actor, settings, notifier):
    _, _, _, user_id, role_code = callback.data.split(":")
    single = await org.single_department(session)
    if single is not None or role_code == "admin":
        dept_id = 0 if role_code == "admin" else single.id
        callback_data = f"p:a:rg:{user_id}:{role_code}:{dept_id}"
        await role_grant(callback.model_copy(update={"data": callback_data}), session, actor, settings, notifier)
        return
    rows = [[("🌐 Все филиалы", f"p:a:rg:{user_id}:{role_code}:0")]]
    for dept in await org.list_departments(session):
        rows.append([(f"🏢 {dept.name}", f"p:a:rg:{user_id}:{role_code}:{dept.id}")])
    rows.append([("⬅️ Назад", "p:a")])
    role_name = (await role_names(session)).get(role_code, role_code)
    show(callback, notifier, f"Роль {h(role_name)}: в каком филиале?", kb(rows))


@router.callback_query(F.data.startswith("p:a:rg:"))
async def role_grant(callback: CallbackQuery, session, actor, settings, notifier):
    _, _, _, user_id, role_code, dept_id = callback.data.split(":")
    user = await session.get(User, int(user_id))
    dept = await session.get(Department, int(dept_id)) if int(dept_id) else None
    await access.grant_role(session, actor, user, role_code, dept)
    await session.flush()
    role_name = (await role_names(session)).get(role_code, role_code)
    notifier.toast(callback, "Роль выдана")
    notifier.send(user.telegram_id, f"Вам выдана роль {h(role_name)}. Справка: /help")
    emp = (await session.execute(select(Employee).where(Employee.user_id == user.id))).scalar_one_or_none()
    if emp:
        show(callback, notifier, *await employee_view(session, settings, emp.id))
    else:
        show(callback, notifier, *await access_view(session))


@router.callback_query(F.data.startswith("p:a:rv:"))
async def access_revoke_confirm(callback: CallbackQuery, notifier):
    grant_id = callback.data.split(":")[3]
    show(callback, notifier, "Отозвать эту роль?", kb([[("✅ Да, отозвать", f"p:a:ry:{grant_id}"), ("Нет", "p:a")]]))


@router.callback_query(F.data.startswith("p:a:ry:"))
async def access_revoke(callback: CallbackQuery, session, actor, notifier):
    await access.revoke_grant(session, actor, int(callback.data.split(":")[3]))
    await session.flush()
    notifier.toast(callback, "Роль отозвана")
    show(callback, notifier, *await access_view(session))


@router.callback_query(F.data == "p:a:id")
async def access_by_id(callback: CallbackQuery, state: FSMContext, actor, notifier):
    if not actor.can_anywhere(Perm.ACCESS_MANAGE):
        actor.require(Perm.ACCESS_MANAGE)
    await ask(state, "grant_user")
    show(
        callback,
        notifier,
        "Напишите Telegram ID человека (он может узнать его, написав боту /id) или перешлите сюда любое его сообщение.",
        cancel_kb("p:a"),
    )


@form("grant_user")
async def form_grant_user(message, state, session, notifier, **_):
    origin = getattr(message, "forward_origin", None)
    sender = getattr(origin, "sender_user", None) if origin else None
    if sender is not None:
        user = await access.upsert_user(session, sender.id, sender.username, sender.full_name)
    else:
        user = await access.find_user(session, message.text.strip())
    await state.clear()
    await session.flush()
    notifier.answer(
        message,
        f"🔑 Какую роль выдать {h(user.full_name or str(user.telegram_id))}?",
        await role_choice(session, f"p:a:rl:{user.id}", back="p:a"),
    )
