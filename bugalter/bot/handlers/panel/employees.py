"""Сотрудники: список, карточка, добавление по должности, ссылка-приглашение, своя цена.

Сотрудника видно по должности. Ник и Telegram ID показываются только владельцу.
"""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from ....db.models import Department, Employee, Tariff, User, WorkType
from ....domain.errors import DomainError, NotFound
from ....domain.money import currency_label, fmt_money, parse_decimal
from ....domain.permissions import Perm
from ....services import access, fixed_salary, org, payroll, tariffs
from ... import ui
from ...ui import h
from .base import PAGE, ROLES_HELP, ask, can_admin, cancel_kb, form, kb, rate_text, role_choice, role_names, show
from .departments import dept_choice

router = Router(name="panel.employees")

WHO = {
    "none": ("👷 Работник", "видит в личке свою работу, зарплату, может попросить аванс"),
    "operator": ("📝 Оператор", "ещё подтверждает отчёты сотрудников в группах, вносит работу и склад"),
    "accountant": ("🧾 Бухгалтер", "ещё выплаты, авансы, премии и штрафы"),
    "storekeeper": ("📦 Кладовщик", "ещё приход, выдача и остатки товара"),
    "manager": ("👔 Руководитель", "всё сразу, плюс ставки"),
}


async def _users(session, employees: list[Employee]) -> dict[int, User]:
    ids = [e.user_id for e in employees if e.user_id]
    if not ids:
        return {}
    return {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).scalars()}


async def employees_view(session, page: int, actor):
    rows_all = await org.list_employees(session)
    total = len(rows_all)
    chunk = [emp for emp, _ in rows_all[page * PAGE : (page + 1) * PAGE]]
    users = await _users(session, chunk) if actor.is_owner else {}
    lines = ["👥 <b>Сотрудники</b>", "🔗 подключён · ⏳ ещё не открыл ссылку", ""]
    rows = []
    for emp in chunk:
        mark = "🚫" if not emp.is_active else ("🔗" if emp.user_id else "⏳")
        nick = f" · {ui.telegram_nick(users[emp.user_id])}" if emp.user_id in users else ""
        lines.append(f"{mark} {h(emp.full_name)}{h(nick)}")
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
    rows.append([("➕ Добавить", "p:e:add:0")])
    rows.append([("⬅️ В меню", "p:home")])
    return "\n".join(lines), kb(rows)


async def employee_view(session, settings, emp_id: int, actor):
    emp = await session.get(Employee, emp_id)
    if emp is None or emp.deleted_at is not None:
        raise NotFound("Сотрудник удалён")
    lines = [f"👤 <b>{h(emp.full_name)}</b>"]
    if not emp.is_active:
        lines.append("🚫 Уволен (история сохранена)")
    if len(await org.active_departments(session)) > 1:
        depts = [await session.get(Department, d) for d in await org.employee_department_ids(session, emp.id)]
        lines.append("🏢 " + (", ".join(h(d.name) for d in depts) or "не в филиале"))
    if emp.user_id:
        user = await session.get(User, emp.user_id)
        if actor.is_owner:
            lines.append(f"🔗 Telegram: {h(ui.telegram_nick(user))} · ID <code>{user.telegram_id}</code>")
        else:
            lines.append("🔗 Подключён к боту")
        grants = await access.list_grants(session, user.id)
        names = await role_names(session)
        if grants:
            parts = [f"{h(names.get(g.role_code, g.role_code))} ({h(d.name) if d else 'все'})" for g, _, d in grants]
            lines.append("🔑 Роль: " + "; ".join(parts))
        else:
            lines.append("🔑 Работник: видит свою работу и зарплату")
    else:
        lines.append("⏳ Ещё не подключился, отправьте ему ссылку (кнопка ниже)")
    oklad = await fixed_salary.monthly(session, settings, emp)
    if oklad is not None:
        lines.append(f"💼 Оклад: {fmt_money(oklad.rate, oklad.currency)} в месяц")
    preview = await payroll.salary_preview(session, settings, emp)
    if preview.earned or preview.advance:
        lines.append(f"💰 Заработано, не выплачено: {fmt_money(preview.earned, settings.currency)}")
    if preview.advance:
        lines.append(f"💵 Аванс, не вычтен: {fmt_money(preview.advance, settings.currency)}")
    personal = await org.chats_of_employee(session, emp.id)
    if personal:
        lines.append("💬 Его группа: " + ", ".join(h(c.title or str(c.chat_id)) for c in personal))
    rates = (
        await session.execute(
            select(Tariff, WorkType)
            .join(WorkType, WorkType.id == Tariff.work_type_id)
            .where(Tariff.employee_id == emp.id, WorkType.code != fixed_salary.CODE)
            .order_by(Tariff.valid_from.desc(), Tariff.id.desc())
        )
    ).all()
    if rates:
        seen = set()
        lines.append("💰 Свои цены:")
        for tariff, wt in rates:
            if wt.id in seen:
                continue
            seen.add(wt.id)
            price = rate_text(tariff.rate, tariff.currency, wt.calc_method)
            lines.append(f"   • {h(wt.name)}: {price} с {tariff.valid_from:%d.%m}")

    rows = []
    if not emp.user_id:
        rows.append([("🔗 Ссылка", f"p:e:inv:{emp.id}"), ("✏️ Должность", f"p:e:pos:{emp.id}")])
    else:
        rows.append([("🔑 Роль", f"p:e:role:{emp.id}"), ("✏️ Должность", f"p:e:pos:{emp.id}")])
    rows.append([("💸 Выплаты", f"p:pay:v:{emp.id}"), ("💰 Своя цена", f"p:e:rate:{emp.id}")])
    rows.append([("💼 Оклад", f"p:e:okl:{emp.id}")])
    if len(await org.active_departments(session)) > 1:
        rows.append([("🏢 + Филиал", f"p:e:ad:{emp.id}"), ("🏢 − Филиал", f"p:e:rd:{emp.id}")])
    if emp.is_active:
        rows.append([("🚫 Уволить", f"p:e:off:{emp.id}")])
    else:
        back = [("♻️ Вернуть", f"p:e:on:{emp.id}")]
        rows.append([*back, ("🗑 Удалить", f"p:e:del:{emp.id}")] if actor.is_owner else back)
    rows.append([("⬅️ Сотрудники", "p:e")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "p:e")
@router.callback_query(F.data.startswith("p:e:l:"))
async def employees(callback: CallbackQuery, state: FSMContext, session, actor, notifier):
    await state.clear()
    page = int(callback.data.split(":")[3]) if callback.data.startswith("p:e:l:") else 0
    show(callback, notifier, *await employees_view(session, page, actor))


@router.callback_query(F.data.startswith("p:e:v:"))
async def employee(callback: CallbackQuery, state: FSMContext, session, actor, settings, notifier):
    await state.clear()
    show(callback, notifier, *await employee_view(session, settings, int(callback.data.split(":")[3]), actor))


@router.callback_query(F.data.startswith("p:e:add:"))
async def employee_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    if not actor.can_anywhere(Perm.ORG_MANAGE):
        actor.require(Perm.ORG_MANAGE)
    dept_id = int(callback.data.split(":")[3])
    await ask(state, "emp_title", dept_id=dept_id or None)
    show(
        callback,
        notifier,
        "✏️ Какая должность? Например: <b>Курьер</b>, <b>Фасовщик</b>, <b>Продавец</b>\n\n"
        "Имя писать не нужно: когда человек откроет ссылку, его ник и ID будут видны вам в карточке. "
        "Остальные видят только должность.",
        cancel_kb("p:e"),
    )


async def invite_text(bot: Bot, session, actor, emp: Employee, role_code: str | None = None) -> str:
    token = await org.create_invite(session, actor, emp, role_code=role_code)
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=inv_{token}"
    return (
        f"🔗 <b>Ссылка для: {h(emp.full_name)}</b>\n{link}\n\n"
        "Перешлите её человеку. Он откроет её, нажмёт Запустить, и у него появится меню. "
        "Ссылка одноразовая, действует 7 дней."
    )


@form("emp_title")
async def form_emp_title(message, state, session, actor, settings, notifier, data, **_):
    emp = await org.create_employee_by_title(session, actor, message.text)
    await state.clear()
    dept = (
        await session.get(Department, data["dept_id"]) if data.get("dept_id") else await org.single_department(session)
    )
    if dept is not None:
        await org.attach_to_department(session, actor, emp, dept)
    await session.flush()
    rows = [[(title, f"p:e:who:{emp.id}:{code}")] for code, (title, _hint) in WHO.items()]
    hints = "\n".join(f"{title}: {hint}" for title, hint in WHO.values())
    notifier.answer(message, f"✅ Добавлен: <b>{h(emp.full_name)}</b>\nЧто он может в боте?\n\n{hints}", kb(rows))


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
    show(callback, notifier, text, kb([[("👤 Карточка", f"p:e:v:{emp.id}"), ("⬅️ Сотрудники", "p:e")]]))


@router.callback_query(F.data.startswith("p:e:inv:"))
async def employee_invite(callback: CallbackQuery, session, actor, notifier, bot: Bot):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    text = await invite_text(bot, session, actor, emp)
    show(callback, notifier, text, kb([[("⬅️ Карточка", f"p:e:v:{emp.id}")]]))


@router.callback_query(F.data.startswith("p:e:ad:"))
async def employee_attach(callback: CallbackQuery, session, actor, settings, notifier):
    parts = callback.data.split(":")
    emp = await session.get(Employee, int(parts[3]))
    if len(parts) == 4:
        current = set(await org.employee_department_ids(session, emp.id))
        show(
            callback,
            notifier,
            f"В какой филиал добавить {h(emp.full_name)}?",
            await dept_choice(session, f"p:e:ad:{emp.id}", exclude=current),
        )
        return
    await org.attach_to_department(session, actor, emp, await session.get(Department, int(parts[4])))
    await session.flush()
    show(callback, notifier, *await employee_view(session, settings, emp.id, actor))


@router.callback_query(F.data.startswith("p:e:rd:"))
async def employee_detach(callback: CallbackQuery, session, actor, settings, notifier):
    parts = callback.data.split(":")
    emp = await session.get(Employee, int(parts[3]))
    if len(parts) == 4:
        current = set(await org.employee_department_ids(session, emp.id))
        show(
            callback,
            notifier,
            f"Из какого филиала убрать {h(emp.full_name)}?",
            await dept_choice(session, f"p:e:rd:{emp.id}", only=current),
        )
        return
    await org.detach_from_department(session, actor, emp, await session.get(Department, int(parts[4])))
    await session.flush()
    show(callback, notifier, *await employee_view(session, settings, emp.id, actor))


@router.callback_query(F.data.startswith("p:e:off:") | F.data.startswith("p:e:on:"))
async def employee_active(callback: CallbackQuery, session, actor, settings, notifier):
    parts = callback.data.split(":")
    emp = await session.get(Employee, int(parts[3]))
    await org.set_employee_active(session, actor, emp, parts[2] == "on")
    await session.flush()
    show(callback, notifier, *await employee_view(session, settings, emp.id, actor))


@router.callback_query(F.data.startswith("p:e:okl:"))
async def employee_oklad(callback: CallbackQuery, state: FSMContext, session, settings, notifier):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    await ask(state, "emp_oklad", emp_id=emp.id)
    day = settings.payroll_split_day
    show(
        callback,
        notifier,
        f"💼 <b>Оклад: {h(emp.full_name)}</b>\n"
        f"Фиксированная зарплата в месяц, в {currency_label(settings.currency)}. "
        f"Половина начисляется {day}-го, половина в последний день месяца, сама.\n"
        "Премии, штрафы и авансы работают как обычно. "
        "Если человек ещё и делает работу по тегам, она добавится сверху.\n\n"
        "Напишите сумму в месяц, например <b>300000</b>. 0 убирает оклад.",
        cancel_kb(f"p:e:v:{emp.id}"),
    )


@form("emp_oklad")
async def form_emp_oklad(message, state, session, actor, settings, notifier, data, **_):
    emp = await session.get(Employee, data["emp_id"])
    amount = parse_decimal(message.text, allow_zero=True, max_scale=2, what="сумму")
    await fixed_salary.set_monthly(session, actor, settings, emp, amount)
    await state.clear()
    await session.flush()
    notifier.answer(message, *await employee_view(session, settings, emp.id, actor))


@router.callback_query(F.data.startswith("p:e:del:"))
async def employee_delete_ask(callback: CallbackQuery, session, notifier):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    show(
        callback,
        notifier,
        f"🗑 Удалить <b>{h(emp.full_name)}</b>?\n\n"
        "Он пропадёт из всех списков, его Telegram отвяжется, бот перестанет ему отвечать. "
        "Если по нему уже были работа, выплаты или товар, эти суммы останутся в отчётах: "
        "стереть проведённые деньги нельзя, это защита учёта.",
        kb([[("🗑 Да, удалить", f"p:e:dely:{emp.id}"), ("✖️ Нет", f"p:e:v:{emp.id}")]]),
    )


@router.callback_query(F.data.startswith("p:e:dely:"))
async def employee_delete(callback: CallbackQuery, session, actor, notifier):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    name = emp.full_name
    full = await org.delete_employee(session, actor, emp)
    note = "стёрт полностью" if full else "удалён из списков, суммы остались в отчётах"
    text, markup = await employees_view(session, 0, actor)
    show(callback, notifier, f"🗑 {h(name)}: {note}.\n\n{text}", markup)


@router.callback_query(F.data.startswith("p:e:pos:"))
async def employee_title(callback: CallbackQuery, state: FSMContext, session, notifier):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    await ask(state, "emp_rename", emp_id=emp.id)
    show(
        callback,
        notifier,
        f"✏️ Сейчас: <b>{h(emp.full_name)}</b>\nНапишите новую должность, например <b>Старший курьер</b>",
        cancel_kb(f"p:e:v:{emp.id}"),
    )


@form("emp_rename")
async def form_emp_rename(message, state, session, actor, settings, notifier, data, **_):
    emp = await session.get(Employee, data["emp_id"])
    await org.set_employee_title(session, actor, emp, message.text)
    await state.clear()
    await session.flush()
    notifier.answer(message, *await employee_view(session, settings, emp.id, actor))


@router.callback_query(F.data.startswith("p:e:role:"))
async def employee_role(callback: CallbackQuery, session, notifier):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    show(
        callback,
        notifier,
        f"🔑 Что может <b>{h(emp.full_name)}</b>?\n\n{ROLES_HELP}",
        await role_choice(session, f"p:e:rl:{emp.user_id}", back=f"p:e:v:{emp.id}"),
    )


@router.callback_query(F.data.startswith("p:e:rate:"))
async def employee_rate(callback: CallbackQuery, session, notifier):
    emp_id = int(callback.data.split(":")[3])
    works = [wt for wt in await tariffs.visible_work_types(session) if wt.is_active]
    rows = [[(f"🛠 {wt.name}", f"p:w:x:{wt.id}:{emp_id}")] for wt in works]
    if not rows:
        rows.append([("➕ Добавить работу", "p:w:add")])
    rows.append([("⬅️ Назад", f"p:e:v:{emp_id}")])
    show(callback, notifier, "💰 Для какой работы своя цена?", kb(rows))


async def accept_invite_start(message: Message, token: str, session, actor, notifier) -> None:
    emp, invite = await org.accept_invite(session, actor.user, token)
    role_note = ""
    creator = await session.get(User, invite.created_by) if invite.created_by else None
    creator_actor = await access.actor_for_user(session, creator) if creator is not None else None
    if invite.role_code and creator_actor is not None:
        dept_id = await org.default_department_id(session, emp.id)
        dept = await session.get(Department, dept_id) if dept_id else None
        try:
            await access.grant_role(session, creator_actor, actor.user, invite.role_code, dept)
            role_name = (await role_names(session)).get(invite.role_code, invite.role_code)
            role_note = f"\nРоль: <b>{h(role_name)}</b>."
        except DomainError:
            role_note = "\nРоль выдаст руководитель."
    await session.flush()
    actor = await access.actor_for_user(session, actor.user)
    notifier.answer(
        message,
        f"✅ Готово! Вы подключены: <b>{h(emp.full_name)}</b>.{role_note}\n"
        "Здесь, в личке, ваша работа, зарплата и выплаты (кнопки внизу).",
        await ui.main_menu_for(session, actor, can_admin(actor)),
    )
    if creator is not None:
        who = f" ({h(ui.telegram_nick(actor.user))}, ID {actor.user.telegram_id})" if creator_actor.is_owner else ""
        notifier.send(creator.telegram_id, f"🔗 Подключился: {h(emp.full_name)}{who}.{role_note}")
