"""Сотрудники: список, карточка, добавление, ссылка-приглашение, своя цена, аванс в долг."""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from ....db.models import Department, Employee, Tariff, User, WorkType
from ....domain.errors import DomainError, NotFound, ValidationError
from ....domain.money import currency_label, fmt_money, parse_decimal
from ....domain.permissions import Perm
from ....services import access, org, payroll, tariffs
from ... import ui
from ...ui import h
from .base import PAGE, ROLES_HELP, ask, can_admin, cancel_kb, form, kb, rate_text, role_choice, role_names, show
from .departments import dept_choice

router = Router(name="panel.employees")

WHO = {
    "none": ("👷 Работник", "видит в личке свою работу и зарплату"),
    "operator": ("📝 Оператор", "вносит выполненную работу и склад в группе"),
    "manager": ("👔 Руководитель", "ведёт работу, премии, выплаты"),
}


async def employees_view(session, page: int):
    rows_all = await org.list_employees(session)
    total = len(rows_all)
    chunk = rows_all[page * PAGE : (page + 1) * PAGE]
    lines = ["👥 <b>Сотрудники</b>", "🔗 подключён · ⏳ ещё не открыл ссылку", ""]
    rows = []
    for emp, _depts in chunk:
        mark = "🔗" if emp.user_id else "⏳"
        if not emp.is_active:
            mark = "🚫"
        lines.append(f"{mark} {h(emp.full_name)}")
        rows.append([(f"{mark} {emp.full_name}", f"p:e:v:{emp.id}")])
    if not rows_all:
        lines.append("Пока никого. Добавьте первого 👇")
    nav = []
    if page > 0:
        nav.append(("◀️", f"p:e:l:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(("▶️", f"p:e:l:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("➕ Добавить сотрудника", "p:e:add:0")])
    rows.append([("⬅️ В меню", "p:home")])
    return "\n".join(lines), kb(rows)


async def employee_view(session, settings, emp_id: int):
    emp = await session.get(Employee, emp_id)
    if emp is None:
        raise NotFound("Сотрудник не найден")
    dept_ids = await org.employee_department_ids(session, emp.id)
    depts = [await session.get(Department, d) for d in dept_ids]
    lines = [f"👤 <b>{h(emp.full_name)}</b>"]
    if not emp.is_active:
        lines.append("🚫 Уволен (история сохранена)")
    lines.append(
        f"💼 Должность: {h(emp.position) if emp.position else 'не указана'} <i>(видна в группе вместо имени)</i>"
    )
    if len(await org.active_departments(session)) > 1:
        lines.append("🏢 " + (", ".join(h(d.name) for d in depts) or "не в филиале"))
    if emp.user_id:
        user = await session.get(User, emp.user_id)
        name = f"@{user.username}" if user.username else str(user.telegram_id)
        lines.append(f"🔗 Telegram: {h(name)}")
        grants = await access.list_grants(session, user.id)
        names = await role_names(session)
        if grants:
            parts = [f"{h(names.get(g.role_code, g.role_code))} ({h(d.name) if d else 'все'})" for g, _, d in grants]
            lines.append("🔑 Роли: " + "; ".join(parts))
        else:
            lines.append("🔑 Работник, видит свою работу и зарплату")
    else:
        lines.append("⏳ Ещё не подключился, отправьте ему ссылку (кнопка ниже)")
    credit = await payroll.credit_limit(session, settings, emp.id, settings.currency)
    lines.append(f"💳 Аванс в долг: {'до ' + fmt_money(credit, settings.currency) if credit else 'нельзя'}")
    personal = await org.chats_of_employee(session, emp.id)
    if personal:
        lines.append("💬 Личная группа: " + ", ".join(h(c.title or str(c.chat_id)) for c in personal))
    rates = (
        await session.execute(
            select(Tariff, WorkType)
            .join(WorkType, WorkType.id == Tariff.work_type_id)
            .where(Tariff.employee_id == emp.id)
            .order_by(Tariff.valid_from.desc(), Tariff.id.desc())
        )
    ).all()
    if rates:
        seen = set()
        lines.append("💰 Индивидуальные ставки:")
        for tariff, wt in rates:
            if wt.id in seen:
                continue
            seen.add(wt.id)
            price = rate_text(tariff.rate, tariff.currency, wt.calc_method)
            lines.append(f"   • {h(wt.name)}: {price} с {tariff.valid_from:%d.%m}")

    rows = []
    if not emp.user_id:
        rows.append([("🔗 Ссылка-приглашение", f"p:e:inv:{emp.id}")])
    else:
        rows.append([("🔑 Роль (что может)", f"p:e:role:{emp.id}")])
    rows.append([("💰 Своя цена", f"p:e:rate:{emp.id}"), ("💳 Аванс в долг", f"p:e:cr:{emp.id}")])
    rows.append([("💼 Должность", f"p:e:pos:{emp.id}")])
    if len(await org.active_departments(session)) > 1:
        rows.append([("🏢 + Филиал", f"p:e:ad:{emp.id}"), ("🏢 − Филиал", f"p:e:rd:{emp.id}")])
    rows.append([("🚫 Уволить", f"p:e:off:{emp.id}") if emp.is_active else ("♻️ Вернуть", f"p:e:on:{emp.id}")])
    rows.append([("⬅️ Сотрудники", "p:e")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "p:e")
@router.callback_query(F.data.startswith("p:e:l:"))
async def employees(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    page = int(callback.data.split(":")[3]) if callback.data.startswith("p:e:l:") else 0
    show(callback, notifier, *await employees_view(session, page))


@router.callback_query(F.data.startswith("p:e:v:"))
async def employee(callback: CallbackQuery, state: FSMContext, session, settings, notifier):
    await state.clear()
    show(callback, notifier, *await employee_view(session, settings, int(callback.data.split(":")[3])))


@router.callback_query(F.data.startswith("p:e:add:"))
async def employee_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    if not actor.can_anywhere(Perm.ORG_MANAGE):
        actor.require(Perm.ORG_MANAGE)
    dept_id = int(callback.data.split(":")[3])
    await ask(state, "emp_name", dept_id=dept_id or None)
    show(callback, notifier, "✏️ Напишите имя и фамилию сотрудника, например: <b>Иван Иванов</b>", cancel_kb("p:e"))


async def invite_text(bot: Bot, session, actor, emp: Employee, role_code: str | None = None) -> str:
    token = await org.create_invite(session, actor, emp, role_code=role_code)
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=inv_{token}"
    return (
        f"🔗 <b>Ссылка для {h(emp.full_name)}</b>\n{link}\n\n"
        "Перешлите её человеку. Он откроет её и нажмёт кнопку Запустить, и всё, он подключён. "
        "Ссылка одноразовая, действует 7 дней."
    )


@form("emp_name")
async def form_emp_name(message, state, session, actor, settings, notifier, data, bot, **_):
    name = " ".join(message.text.split())
    if len(name) < 2:
        raise ValidationError("Слишком короткое имя")
    code = await org.unique_code(session, Employee.code, name)
    emp = await org.create_employee(session, actor, code, name)
    await state.clear()
    dept = (
        await session.get(Department, data["dept_id"]) if data.get("dept_id") else await org.single_department(session)
    )
    if dept is not None:
        await org.attach_to_department(session, actor, emp, dept)
    await session.flush()
    rows = [[(title, f"p:e:who:{emp.id}:{code_}")] for code_, (title, _hint) in WHO.items()]
    hints = "\n".join(f"{title}: {hint}" for title, hint in WHO.values())
    notifier.answer(message, f"✅ {h(emp.full_name)} добавлен. Кто он?\n\n{hints}", kb(rows))


@router.callback_query(F.data.startswith("p:e:who:"))
async def employee_who(callback: CallbackQuery, session, actor, settings, notifier, bot: Bot):
    _, _, _, emp_id, role = callback.data.split(":")
    emp = await session.get(Employee, int(emp_id))
    text = await invite_text(bot, session, actor, emp, None if role == "none" else role)
    if role != "none":
        text += f"\nКогда подключится, получит роль {WHO[role][0][2:]}."
    if not await org.employee_department_ids(session, emp.id):
        show(
            callback,
            notifier,
            text + f"\n\nВ какой филиал добавить {h(emp.full_name)}?",
            await dept_choice(session, f"p:e:ad:{emp.id}"),
        )
        return
    show(callback, notifier, text, kb([[("👤 Карточка сотрудника", f"p:e:v:{emp.id}")], [("⬅️ Сотрудники", "p:e")]]))


@router.callback_query(F.data.startswith("p:e:inv:"))
async def employee_invite(callback: CallbackQuery, session, actor, notifier, bot: Bot):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    text = await invite_text(bot, session, actor, emp)
    show(callback, notifier, text, kb([[("⬅️ К сотруднику", f"p:e:v:{emp.id}")]]))


@router.callback_query(F.data.startswith("p:e:ad:"))
async def employee_attach(callback: CallbackQuery, session, actor, settings, notifier):
    parts = callback.data.split(":")
    emp = await session.get(Employee, int(parts[3]))
    if len(parts) == 4:
        current = set(await org.employee_department_ids(session, emp.id))
        show(
            callback,
            notifier,
            f"В какое подразделение добавить {h(emp.full_name)}?",
            await dept_choice(session, f"p:e:ad:{emp.id}", exclude=current),
        )
        return
    await org.attach_to_department(session, actor, emp, await session.get(Department, int(parts[4])))
    await session.flush()
    show(callback, notifier, *await employee_view(session, settings, emp.id))


@router.callback_query(F.data.startswith("p:e:rd:"))
async def employee_detach(callback: CallbackQuery, session, actor, settings, notifier):
    parts = callback.data.split(":")
    emp = await session.get(Employee, int(parts[3]))
    if len(parts) == 4:
        current = set(await org.employee_department_ids(session, emp.id))
        show(
            callback,
            notifier,
            f"Из какого подразделения убрать {h(emp.full_name)}?",
            await dept_choice(session, f"p:e:rd:{emp.id}", only=current),
        )
        return
    await org.detach_from_department(session, actor, emp, await session.get(Department, int(parts[4])))
    await session.flush()
    show(callback, notifier, *await employee_view(session, settings, emp.id))


@router.callback_query(F.data.startswith("p:e:off:") | F.data.startswith("p:e:on:"))
async def employee_active(callback: CallbackQuery, session, actor, settings, notifier):
    parts = callback.data.split(":")
    emp = await session.get(Employee, int(parts[3]))
    await org.set_employee_active(session, actor, emp, parts[2] == "on")
    await session.flush()
    show(callback, notifier, *await employee_view(session, settings, emp.id))


@router.callback_query(F.data.startswith("p:e:cr:"))
async def employee_credit(callback: CallbackQuery, state: FSMContext, session, settings, notifier):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    await ask(state, "emp_credit", emp_id=emp.id)
    show(
        callback,
        notifier,
        f"💳 Сколько можно выдать {h(emp.full_name)} <b>сверх заработанного</b> (аванс в долг), "
        f"в {currency_label(settings.currency)}?\nНапишите число, например <b>500</b>. 0 значит запретить.\n"
        "Долг автоматически гасится из следующих начислений.",
        cancel_kb(f"p:e:v:{emp.id}"),
    )


@form("emp_credit")
async def form_emp_credit(message, state, session, actor, settings, notifier, data, **_):
    emp = await session.get(Employee, data["emp_id"])
    amount = parse_decimal(message.text, allow_zero=True, max_scale=2, what="сумму")
    await payroll.set_credit_limit(session, actor, settings, emp, amount, settings.currency)
    await state.clear()
    await session.flush()
    notifier.answer(message, *await employee_view(session, settings, emp.id))


@router.callback_query(F.data.startswith("p:e:pos:"))
async def employee_position(callback: CallbackQuery, state: FSMContext, session, notifier):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    await ask(state, "emp_position", emp_id=emp.id)
    show(
        callback,
        notifier,
        f"💼 Должность {h(emp.full_name)}, например <b>Курьер</b>.\n"
        "Её видят все в рабочей группе вместо имени. Отправьте - (дефис), чтобы убрать.",
        cancel_kb(f"p:e:v:{emp.id}"),
    )


@form("emp_position")
async def form_emp_position(message, state, session, actor, settings, notifier, data, **_):
    emp = await session.get(Employee, data["emp_id"])
    position = None if message.text.strip() == "-" else message.text
    await org.set_employee_position(session, actor, emp, position)
    await state.clear()
    await session.flush()
    notifier.answer(message, *await employee_view(session, settings, emp.id))


@router.callback_query(F.data.startswith("p:e:role:"))
async def employee_role(callback: CallbackQuery, session, notifier):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    show(
        callback,
        notifier,
        f"🔑 Какую роль выдать {h(emp.full_name)}?\n\n{ROLES_HELP}",
        await role_choice(session, f"p:e:rl:{emp.user_id}", back=f"p:e:v:{emp.id}"),
    )


@router.callback_query(F.data.startswith("p:e:rate:"))
async def employee_rate(callback: CallbackQuery, session, notifier):
    emp_id = int(callback.data.split(":")[3])
    rows = [
        [(f"🛠 {wt.name}", f"p:w:x:{wt.id}:{emp_id}")] for wt in await tariffs.list_work_types(session) if wt.is_active
    ]
    if not rows:
        rows.append([("➕ Добавить работу", "p:w:add")])
    rows.append([("⬅️ Назад", f"p:e:v:{emp_id}")])
    show(callback, notifier, "💰 Для какой работы своя цена?", kb(rows))


async def accept_invite_start(message: Message, token: str, session, actor, notifier) -> None:
    emp, invite = await org.accept_invite(session, actor.user, token)
    role_note = ""
    creator = await session.get(User, invite.created_by) if invite.created_by else None
    if invite.role_code and creator is not None:
        creator_actor = await access.actor_for_user(session, creator)
        dept_id = await org.default_department_id(session, emp.id)
        dept = await session.get(Department, dept_id) if dept_id else None
        try:
            await access.grant_role(session, creator_actor, actor.user, invite.role_code, dept)
            role_name = (await role_names(session)).get(invite.role_code, invite.role_code)
            role_note = f"\nВаша роль: <b>{h(role_name)}</b>."
        except DomainError:
            role_note = "\nРоль выдаст руководитель."
    await session.flush()
    actor = await access.actor_for_user(session, actor.user)
    notifier.answer(
        message,
        f"✅ Готово! Вы подключены как <b>{h(emp.full_name)}</b>.{role_note}\n"
        "Здесь, в личке, ваша работа, зарплата и выплаты (кнопки внизу).",
        ui.main_menu(can_admin(actor)),
    )
    if creator is not None:
        notifier.send(creator.telegram_id, f"🔗 {h(emp.full_name)} подключился к боту.{role_note}")
