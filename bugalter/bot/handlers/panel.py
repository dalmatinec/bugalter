"""Панель управления на кнопках: /admin или кнопка Управление в личке с ботом.

Всё настраивается кнопками и короткими ответами, коды придумывать не нужно, бот создаёт их сам.
"""

import datetime as dt
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from ...db.models import (
    AuditLog,
    Department,
    Employee,
    Product,
    Role,
    Tariff,
    User,
    WorkType,
)
from ...domain.errors import AccessDenied, DomainError, NotFound, ValidationError
from ...domain.money import currency_label, fmt_money, fmt_number, fmt_price, fmt_qty, parse_decimal
from ...domain.permissions import OWNER_ROLE, Perm
from ...services import access, org, payroll, reference, stock, tariffs, work
from ...services import settings as settings_service
from ...services.settings import SPECS
from .. import ui
from ..ui import h

router = Router(name="panel")

PAGE = 8

ADMIN_PERMS = (
    Perm.ORG_MANAGE,
    Perm.ACCESS_MANAGE,
    Perm.WORKTYPE_MANAGE,
    Perm.TARIFF_MANAGE,
    Perm.STOCK_MANAGE,
    Perm.SETTINGS_MANAGE,
    Perm.PAYROLL_SIGN,
)

METHOD_TITLES = {
    "per_unit": "за единицу (количество × ставка, можно со ступенями)",
    "fixed": "фиксированная сумма за операцию",
    "percent": "процент от суммы заказа",
}
METHOD_SHORT = {"per_unit": "за единицу", "fixed": "фикс за операцию", "percent": "% от суммы"}

TIMEZONES = [
    ("Москва", "Europe/Moscow"),
    ("Киев", "Europe/Kyiv"),
    ("Минск", "Europe/Minsk"),
    ("Алматы", "Asia/Almaty"),
    ("Ташкент", "Asia/Tashkent"),
    ("Бишкек", "Asia/Bishkek"),
    ("Дубай", "Asia/Dubai"),
    ("Стамбул", "Europe/Istanbul"),
    ("Лондон", "Europe/London"),
    ("Нью-Йорк", "America/New_York"),
    ("UTC", "UTC"),
]
ROUNDING_TITLES = {
    "HALF_UP": "по правилам математики",
    "HALF_EVEN": "банковское",
    "DOWN": "всегда вниз",
    "UP": "всегда вверх",
}
TOGGLES = {
    "group_show_amounts": "👁 Суммы в рабочих группах",
    "allow_self_confirm": "✍️ Автор может сам подписать премию/штраф",
    "payout_requires_confirmation": "💸 Выплату отдельно подтверждает владелец",
    "deduction_requires_consent": "🤝 Удержание только с согласия сотрудника",
}
ACTION_TITLES = {
    "work.create": "внесена операция",
    "work.confirm": "подтверждена операция",
    "work.reject": "отклонена операция",
    "work.cancel": "отменена операция",
    "tariff.create": "новая ставка",
    "payroll.bonus_create": "выписана премия",
    "payroll.penalty_create": "выписан штраф",
    "payroll.deduction_create": "выписано удержание",
    "payroll.sign": "подписан документ",
    "payroll.payout_register": "зарегистрирована выплата",
    "payroll.payout_confirm": "подтверждена выплата",
    "payroll.withhold": "удержана часть",
    "period.close": "закрыт период",
    "period.reopen": "переоткрыт период",
    "org.employee_create": "добавлен сотрудник",
    "org.department_create": "добавлено подразделение",
    "org.chat_bind": "привязана группа",
    "org.invite_accept": "сотрудник подключился по ссылке",
    "access.grant": "выдана роль",
    "access.revoke": "отозвана роль",
    "settings.set": "изменена настройка",
    "stock.receipt": "поступление товара",
    "stock.issue": "выдача товара",
    "stock.accept": "товар получен",
    "stock.inventory": "инвентаризация",
}


class AdminForm(StatesGroup):
    input = State()


def can_admin(actor) -> bool:
    return actor.is_owner or any(actor.can_anywhere(p) for p in ADMIN_PERMS)


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*[_button(text, data) for text, data in row])
    return builder.as_markup()


def _button(text: str, data: str) -> InlineKeyboardButton:
    if data.startswith("http"):
        return InlineKeyboardButton(text=text, url=data)
    return InlineKeyboardButton(text=text, callback_data=data)


def _show(event, notifier, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(event, CallbackQuery):
        notifier.edit(event.message, text, markup)
    else:
        notifier.answer(event, text, markup)


def _private(is_private: bool) -> None:
    if not is_private:
        raise ValidationError("Панель управления открывается в личном чате с ботом")


async def _ask(state: FSMContext, form: str, **data) -> None:
    await state.set_state(AdminForm.input)
    await state.update_data(form=form, **data)


def rate_text(rate: Decimal, currency: str, method: str = "per_unit") -> str:
    if method == "percent":
        return f"{fmt_number(rate)} %"
    return fmt_price(rate, currency)


# ------------------------------------------------------------------ главное меню


HOME_TEXT = """⚙️ <b>Управление</b>

👥 <b>Сотрудники</b>: добавить человека, бот даст ссылку для него
💰 <b>Работы и цены</b>: за что платим и сколько
💬 <b>Группы</b>: как подключить рабочую группу

Остальное (доплаты, склад, настройки, доступ) - в разделе Ещё."""


def home_markup() -> InlineKeyboardMarkup:
    return _kb(
        [
            [("👥 Сотрудники", "p:e"), ("💰 Работы и цены", "p:w")],
            [("💬 Группы", "p:g"), ("➕ Ещё", "p:more")],
        ]
    )


MORE_TEXT = """➕ <b>Ещё</b>

⚡ <b>Доплаты</b>: коэффициенты, например срочно ×1,5 или выходной ×2
📦 <b>Товары</b>: если ведёте склад
⚙️ <b>Настройки</b>: валюта, часовой пояс, периоды выплат, лимиты
🔑 <b>Доступ</b>: кто что может (обычно хватает выбора при добавлении сотрудника)
🏢 <b>Филиалы</b>: только если у вас несколько точек со своими сотрудниками и складом
📜 <b>Журнал</b>: кто что менял"""


def more_markup() -> InlineKeyboardMarkup:
    return _kb(
        [
            [("⚡ Доплаты", "p:c"), ("📦 Товары", "p:pr")],
            [("⚙️ Настройки", "p:s"), ("🔑 Доступ", "p:a")],
            [("🏢 Филиалы", "p:d"), ("📜 Журнал", "p:j")],
            [("⬅️ В меню", "p:home")],
        ]
    )


@router.callback_query(F.data == "p:more")
async def more(callback: CallbackQuery, state: FSMContext, notifier):
    await state.clear()
    _show(callback, notifier, MORE_TEXT, more_markup())


@router.message(Command("admin"))
@router.message(F.text == ui.BTN_PANEL)
async def open_panel(message: Message, state: FSMContext, actor, notifier, is_private):
    _private(is_private)
    if not can_admin(actor):
        raise AccessDenied("Управление доступно владельцу, администратору и руководителям")
    await state.clear()
    notifier.answer(message, HOME_TEXT, home_markup())


@router.callback_query(F.data == "p:home")
async def home(callback: CallbackQuery, state: FSMContext, notifier):
    await state.clear()
    _show(callback, notifier, HOME_TEXT, home_markup())


# ------------------------------------------------------------------ подразделения


async def departments_view(session):
    depts = await org.list_departments(session)
    counts = await org.department_counts(session)
    lines = [
        "🏢 <b>Филиалы</b>",
        "Филиал - это отдельная точка: магазин, склад или город со своими сотрудниками, складом и отчётами.",
        "<b>Если точка одна, здесь ничего делать не нужно</b>, всё уже настроено.",
        "",
    ]
    rows = []
    for dept in depts:
        members, chats = counts.get(dept.id, (0, 0))
        lines.append(f"• <b>{h(dept.name)}</b>: сотрудников {members}, групп {chats}")
        rows.append([(f"🏢 {dept.name}", f"p:d:v:{dept.id}")])
    rows.append([("➕ Добавить филиал", "p:d:add")])
    rows.append([("⬅️ Назад", "p:more")])
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data == "p:d")
async def departments(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    _show(callback, notifier, *await departments_view(session))


async def department_view(session, dept_id: int):
    dept = await session.get(Department, dept_id)
    if dept is None:
        raise NotFound("Подразделение не найдено")
    employees = await org.department_employees(session, dept.id)
    chats = await org.chats_of_department(session, dept.id)
    lines = [f"🏢 <b>{h(dept.name)}</b>  <i>код для команд: {h(dept.code)}</i>", "", "<u>Сотрудники</u>"]
    lines += [f"• {h(e.full_name)}" for e in employees] or ["нет"]
    lines += ["", "<u>Рабочие группы</u>"]
    lines += [f"• {h(c.title or str(c.chat_id))}" for c in chats] or ["нет, добавьте бота в группу"]
    markup = _kb(
        [
            [("➕ Новый сотрудник", f"p:e:add:{dept.id}")],
            [("💬 Подключить группу", "p:g")],
            [("⬅️ Филиалы", "p:d")],
        ]
    )
    return "\n".join(lines), markup


@router.callback_query(F.data.startswith("p:d:v:"))
async def department(callback: CallbackQuery, session, notifier):
    _show(callback, notifier, *await department_view(session, int(callback.data.split(":")[3])))


@router.callback_query(F.data == "p:d:add")
async def department_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    actor.require(Perm.ORG_MANAGE)
    await _ask(state, "dept_name")
    _show(
        callback,
        notifier,
        "✏️ Напишите название филиала, например: <b>Север</b> или <b>Склад на Ленина</b>",
        _cancel_kb("p:d"),
    )


def _cancel_kb(back: str) -> InlineKeyboardMarkup:
    return _kb([[("✖️ Отмена", back)]])


async def form_dept_name(message, state, session, actor, notifier, data, **_):
    name = message.text.strip()
    code = await org.unique_code(session, Department.code, name)
    dept = await org.create_department(session, actor, code, name)
    await state.clear()
    await session.flush()
    notifier.answer(message, f"✅ Филиал {h(dept.name)} создан.", None)
    notifier.answer(message, *await department_view(session, dept.id))


# ------------------------------------------------------------------ сотрудники


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
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data == "p:e")
@router.callback_query(F.data.startswith("p:e:l:"))
async def employees(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    page = int(callback.data.split(":")[3]) if callback.data.startswith("p:e:l:") else 0
    _show(callback, notifier, *await employees_view(session, page))


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
        role_names = await _role_names(session)
        if grants:
            lines.append(
                "🔑 Роли: "
                + "; ".join(
                    f"{h(role_names.get(g.role_code, g.role_code))} ({h(d.name) if d else 'все'})" for g, _, d in grants
                )
            )
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
            lines.append(
                f"   • {h(wt.name)}: {rate_text(tariff.rate, tariff.currency, wt.calc_method)} с {tariff.valid_from:%d.%m}"
            )

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
    return "\n".join(lines), _kb(rows)


async def _role_names(session) -> dict[str, str]:
    return dict((await session.execute(select(Role.code, Role.name))).all())


@router.callback_query(F.data.startswith("p:e:v:"))
async def employee(callback: CallbackQuery, state: FSMContext, session, settings, notifier):
    await state.clear()
    _show(callback, notifier, *await employee_view(session, settings, int(callback.data.split(":")[3])))


@router.callback_query(F.data.startswith("p:e:add:"))
async def employee_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    if not actor.can_anywhere(Perm.ORG_MANAGE):
        actor.require(Perm.ORG_MANAGE)
    dept_id = int(callback.data.split(":")[3])
    await _ask(state, "emp_name", dept_id=dept_id or None)
    _show(callback, notifier, "✏️ Напишите имя и фамилию сотрудника, например: <b>Иван Иванов</b>", _cancel_kb("p:e"))


WHO = {
    "none": ("👷 Работник", "видит в личке свою работу и зарплату"),
    "operator": ("📝 Оператор", "вносит выполненную работу и склад в группе"),
    "manager": ("👔 Руководитель", "ведёт работу, премии, выплаты"),
}


async def _invite_text(bot: Bot, session, actor, emp: Employee, role_code: str | None = None) -> str:
    token = await org.create_invite(session, actor, emp, role_code=role_code)
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=inv_{token}"
    return (
        f"🔗 <b>Ссылка для {h(emp.full_name)}</b>\n{link}\n\n"
        "Перешлите её человеку. Он откроет её и нажмёт кнопку Запустить, и всё, он подключён. "
        "Ссылка одноразовая, действует 7 дней."
    )


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
    notifier.answer(message, f"✅ {h(emp.full_name)} добавлен. Кто он?\n\n{hints}", _kb(rows))


@router.callback_query(F.data.startswith("p:e:who:"))
async def employee_who(callback: CallbackQuery, session, actor, settings, notifier, bot: Bot):
    _, _, _, emp_id, role = callback.data.split(":")
    emp = await session.get(Employee, int(emp_id))
    text = await _invite_text(bot, session, actor, emp, None if role == "none" else role)
    if role != "none":
        text += f"\nКогда подключится, получит роль {WHO[role][0][2:]}."
    if not await org.employee_department_ids(session, emp.id):
        _show(
            callback,
            notifier,
            text + f"\n\nВ какой филиал добавить {h(emp.full_name)}?",
            await _dept_choice(session, f"p:e:ad:{emp.id}"),
        )
        return
    _show(callback, notifier, text, _kb([[("👤 Карточка сотрудника", f"p:e:v:{emp.id}")], [("⬅️ Сотрудники", "p:e")]]))


async def _dept_choice(session, prefix: str, exclude: set[int] | None = None, only: set[int] | None = None):
    rows = []
    for dept in await org.list_departments(session):
        if exclude and dept.id in exclude:
            continue
        if only is not None and dept.id not in only:
            continue
        rows.append([(f"🏢 {dept.name}", f"{prefix}:{dept.id}")])
    if not rows:
        rows.append([("➕ Создать филиал", "p:d:add")])
    rows.append([("⬅️ Назад", "p:e")])
    return _kb(rows)


@router.callback_query(F.data.startswith("p:e:inv:"))
async def employee_invite(callback: CallbackQuery, session, actor, notifier, bot: Bot):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    text = await _invite_text(bot, session, actor, emp)
    _show(callback, notifier, text, _kb([[("⬅️ К сотруднику", f"p:e:v:{emp.id}")]]))


@router.callback_query(F.data.startswith("p:e:ad:"))
async def employee_attach(callback: CallbackQuery, session, actor, settings, notifier):
    parts = callback.data.split(":")
    emp = await session.get(Employee, int(parts[3]))
    if len(parts) == 4:
        current = set(await org.employee_department_ids(session, emp.id))
        _show(
            callback,
            notifier,
            f"В какое подразделение добавить {h(emp.full_name)}?",
            await _dept_choice(session, f"p:e:ad:{emp.id}", exclude=current),
        )
        return
    await org.attach_to_department(session, actor, emp, await session.get(Department, int(parts[4])))
    await session.flush()
    _show(callback, notifier, *await employee_view(session, settings, emp.id))


@router.callback_query(F.data.startswith("p:e:rd:"))
async def employee_detach(callback: CallbackQuery, session, actor, settings, notifier):
    parts = callback.data.split(":")
    emp = await session.get(Employee, int(parts[3]))
    if len(parts) == 4:
        current = set(await org.employee_department_ids(session, emp.id))
        _show(
            callback,
            notifier,
            f"Из какого подразделения убрать {h(emp.full_name)}?",
            await _dept_choice(session, f"p:e:rd:{emp.id}", only=current),
        )
        return
    await org.detach_from_department(session, actor, emp, await session.get(Department, int(parts[4])))
    await session.flush()
    _show(callback, notifier, *await employee_view(session, settings, emp.id))


@router.callback_query(F.data.startswith("p:e:off:") | F.data.startswith("p:e:on:"))
async def employee_active(callback: CallbackQuery, session, actor, settings, notifier):
    parts = callback.data.split(":")
    emp = await session.get(Employee, int(parts[3]))
    await org.set_employee_active(session, actor, emp, parts[2] == "on")
    await session.flush()
    _show(callback, notifier, *await employee_view(session, settings, emp.id))


@router.callback_query(F.data.startswith("p:e:cr:"))
async def employee_credit(callback: CallbackQuery, state: FSMContext, session, settings, notifier):
    emp = await session.get(Employee, int(callback.data.split(":")[3]))
    await _ask(state, "emp_credit", emp_id=emp.id)
    _show(
        callback,
        notifier,
        f"💳 Сколько можно выдать {h(emp.full_name)} <b>сверх заработанного</b> (аванс в долг), "
        f"в {currency_label(settings.currency)}?\nНапишите число, например <b>500</b>. 0 значит запретить.\n"
        "Долг автоматически гасится из следующих начислений.",
        _cancel_kb(f"p:e:v:{emp.id}"),
    )


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
    await _ask(state, "emp_position", emp_id=emp.id)
    _show(
        callback,
        notifier,
        f"💼 Должность {h(emp.full_name)}, например <b>Курьер</b>.\n"
        "Её видят все в рабочей группе вместо имени. Отправьте - (дефис), чтобы убрать.",
        _cancel_kb(f"p:e:v:{emp.id}"),
    )


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
    _show(
        callback,
        notifier,
        f"🔑 Какую роль выдать {h(emp.full_name)}?\n\n{await _roles_help(session)}",
        await _role_choice(session, f"p:e:rl:{emp.user_id}", back=f"p:e:v:{emp.id}"),
    )


async def _roles_help(session) -> str:
    return (
        "• <b>Оператор</b>: вносит операции и складские движения в группе\n"
        "• <b>Руководитель</b>: ведёт операции, премии и штрафы, выплаты, ставки своего подразделения "
        "(подтверждает операции и выплаты только владелец)\n"
        "• <b>Кладовщик</b>: склад и инвентаризация\n"
        "• <b>Бухгалтер</b>: выплаты, премии, отчёты\n"
        "• <b>Администратор</b>: всё, кроме прав владельца\n"
        "Сотруднику без роли бот показывает только его работу и расчёт."
    )


async def _role_choice(session, prefix: str, back: str):
    rows = []
    roles = (await session.execute(select(Role).order_by(Role.code))).scalars()
    for role in roles:
        if role.code == OWNER_ROLE:
            continue
        rows.append([(role.name, f"{prefix}:{role.code}")])
    rows.append([("⬅️ Назад", back)])
    return _kb(rows)


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
    role_name = (await _role_names(session)).get(role_code, role_code)
    _show(callback, notifier, f"Роль {h(role_name)}: в каком филиале?", _kb(rows))


@router.callback_query(F.data.startswith("p:a:rg:"))
async def role_grant(callback: CallbackQuery, session, actor, settings, notifier):
    _, _, _, user_id, role_code, dept_id = callback.data.split(":")
    user = await session.get(User, int(user_id))
    dept = await session.get(Department, int(dept_id)) if int(dept_id) else None
    await access.grant_role(session, actor, user, role_code, dept)
    await session.flush()
    role_name = (await _role_names(session)).get(role_code, role_code)
    notifier.toast(callback, "Роль выдана")
    notifier.send(
        user.telegram_id,
        f"Вам выдана роль {h(role_name)}. Справка: /help",
    )
    emp = (await session.execute(select(Employee).where(Employee.user_id == user.id))).scalar_one_or_none()
    if emp:
        _show(callback, notifier, *await employee_view(session, settings, emp.id))
    else:
        _show(callback, notifier, *await access_view(session))


@router.callback_query(F.data.startswith("p:e:rate:"))
async def employee_rate(callback: CallbackQuery, session, notifier):
    emp_id = int(callback.data.split(":")[3])
    rows = [
        [(f"🛠 {wt.name}", f"p:w:x:{wt.id}:{emp_id}")] for wt in await tariffs.list_work_types(session) if wt.is_active
    ]
    if not rows:
        rows.append([("➕ Добавить работу", "p:w:add")])
    rows.append([("⬅️ Назад", f"p:e:v:{emp_id}")])
    _show(callback, notifier, "💰 Для какой работы своя цена?", _kb(rows))


# ------------------------------------------------------------------ роли и доступ


async def access_view(session):
    grants = await access.list_grants(session)
    role_names = await _role_names(session)
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
        role_name = role_names.get(g.role_code, g.role_code)
        lines.append(f"• {h(who)}: <b>{h(role_name)}</b> ({h(scope)})")
        if g.role_code != OWNER_ROLE:
            rows.append([(f"❌ {who}: {role_name}", f"p:a:rv:{g.id}")])
    rows.append([("➕ Выдать роль по ID", "p:a:id")])
    rows.append([("📖 Про роли", "p:a:help")])
    rows.append([("⬅️ Назад", "p:more")])
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data == "p:a")
async def access_section(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    _show(callback, notifier, *await access_view(session))


@router.callback_query(F.data == "p:a:help")
async def access_help(callback: CallbackQuery, session, notifier):
    _show(callback, notifier, "📖 <b>Роли</b>\n\n" + await _roles_help(session), _kb([[("⬅️ Назад", "p:a")]]))


@router.callback_query(F.data.startswith("p:a:rv:"))
async def access_revoke_confirm(callback: CallbackQuery, notifier):
    grant_id = callback.data.split(":")[3]
    _show(callback, notifier, "Отозвать эту роль?", _kb([[("✅ Да, отозвать", f"p:a:ry:{grant_id}"), ("Нет", "p:a")]]))


@router.callback_query(F.data.startswith("p:a:ry:"))
async def access_revoke(callback: CallbackQuery, session, actor, notifier):
    await access.revoke_grant(session, actor, int(callback.data.split(":")[3]))
    await session.flush()
    notifier.toast(callback, "Роль отозвана")
    _show(callback, notifier, *await access_view(session))


@router.callback_query(F.data == "p:a:id")
async def access_by_id(callback: CallbackQuery, state: FSMContext, actor, notifier):
    if not actor.can_anywhere(Perm.ACCESS_MANAGE):
        actor.require(Perm.ACCESS_MANAGE)
    await _ask(state, "grant_user")
    _show(
        callback,
        notifier,
        "Напишите Telegram ID человека (он может узнать его, написав боту /id) или перешлите сюда любое его сообщение.",
        _cancel_kb("p:a"),
    )


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
        await _role_choice(session, f"p:a:rl:{user.id}", back="p:a"),
    )


# ------------------------------------------------------------------ работы и ставки


async def works_view(session, settings):
    lines = ["💰 <b>Работы и цены</b>", "За что платим и сколько. Нажмите на работу, чтобы поменять цену.", ""]
    rows = []
    today = settings.today()
    for wt in await tariffs.list_work_types(session):
        rates = [r for r in await tariffs.current_rates(session, wt.id, today) if not r[4]]
        base = next((r for r in rates if r[1] is None and r[2] is None), rates[0] if rates else None)
        price = rate_text(base[0].rate, base[0].currency, wt.calc_method) if base else "цена не задана ⚠️"
        if base and wt.calc_method == "per_unit":
            price += f" за {h(wt.unit_code)}"
        elif base and wt.calc_method == "fixed":
            price += " за раз"
        state = "" if wt.is_active else " (выключена)"
        lines.append(f"• <b>{h(wt.name)}</b>{state}: {price}")
        rows.append([(f"{wt.name}", f"p:w:v:{wt.id}")])
    if not rows:
        lines.append("Пока пусто. Добавьте первую работу, например Доставка 👇")
    rows.append([("➕ Добавить работу", "p:w:add")])
    rows.append([("⬅️ В меню", "p:home")])
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data == "p:w")
async def works(callback: CallbackQuery, state: FSMContext, session, settings, notifier):
    await state.clear()
    _show(callback, notifier, *await works_view(session, settings))


async def work_view(session, settings, wt_id: int):
    wt = await session.get(WorkType, wt_id)
    if wt is None:
        raise NotFound("Вид работ не найден")
    today = settings.today()
    lines = [
        f"💰 <b>{h(wt.name)}</b>",
        f"Оплата: {METHOD_TITLES[wt.calc_method]}"
        + (f", считаем в {h(wt.unit_code)}" if wt.calc_method == "per_unit" else ""),
    ]
    if not wt.is_active:
        lines.append("⏸ Выключена, при вводе работы не показывается")
    lines += ["", "<u>Цены</u>"]
    rates = await tariffs.current_rates(session, wt.id, today)
    single_dept = len(await org.active_departments(session)) == 1
    for tariff, dept_name, emp_name, tiers, future in rates:
        scope = emp_name or dept_name or "для всех"
        if dept_name and not emp_name and single_dept:
            scope = "для всех"
        if emp_name and dept_name:
            scope = f"{emp_name} ({dept_name})"
        line = f"• {h(scope)}: <b>{rate_text(tariff.rate, tariff.currency, wt.calc_method)}</b>"
        if tiers:
            line += " · ступени: " + ", ".join(
                f"сверх {fmt_number(t)}: {rate_text(r, tariff.currency)}" for t, r in tiers
            )
        line += f" с {tariff.valid_from:%d.%m.%Y}" if future else ""
        if future:
            line += " (будущая)"
        lines.append(line)
    if not rates:
        lines.append("⚠️ Цена не задана, внести эту работу пока нельзя")
    if any(r[2] for r in rates):
        lines.append("\nСвоя цена сотрудника важнее общей.")
    rows = [
        [("💰 Изменить цену", f"p:w:s:{wt.id}:0"), ("👤 Цена сотруднику", f"p:w:e:{wt.id}")],
    ]
    if len(await org.active_departments(session)) > 1:
        rows.append([("🏢 Цена филиала", f"p:w:t:{wt.id}")])
    rows.append([("⏸ Выключить" if wt.is_active else "▶️ Включить", f"p:w:tg:{wt.id}")])
    rows.append([("⬅️ Работы и цены", "p:w")])
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data.startswith("p:w:v:"))
async def work_type(callback: CallbackQuery, state: FSMContext, session, settings, notifier):
    await state.clear()
    _show(callback, notifier, *await work_view(session, settings, int(callback.data.split(":")[3])))


@router.callback_query(F.data.startswith("p:w:tg:"))
async def work_toggle(callback: CallbackQuery, session, actor, settings, notifier):
    wt = await session.get(WorkType, int(callback.data.split(":")[3]))
    await tariffs.set_work_type_active(session, actor, wt, not wt.is_active)
    await session.flush()
    _show(callback, notifier, *await work_view(session, settings, wt.id))


@router.callback_query(F.data == "p:w:add")
async def work_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    actor.require(Perm.WORKTYPE_MANAGE)
    await _ask(state, "wt_name")
    _show(
        callback,
        notifier,
        "✏️ Как называется работа? Например: <b>Доставка</b>, <b>Фасовка</b>, <b>Погрузка</b>",
        _cancel_kb("p:w"),
    )


PAY_BUTTONS = [
    ("📦 За штуку / заказ / кг / час", "per_unit"),
    ("💵 Фиксированно за раз", "fixed"),
    ("％ Процент от суммы заказа", "percent"),
]
COMMON_UNITS = ("заказ", "шт", "кг", "ч", "адрес", "заявка")


async def form_wt_name(message, state, session, notifier, **_):
    name = " ".join(message.text.split())
    await state.update_data(wt_name=name)
    rows = [[(title, f"p:w:m:{code}")] for title, code in PAY_BUTTONS]
    rows.append([("✖️ Отмена", "p:w")])
    notifier.answer(message, f"{h(name)}: как платим?", _kb(rows))


@router.callback_query(F.data.startswith("p:w:m:"))
async def work_method(callback: CallbackQuery, state: FSMContext, session, actor, settings, notifier):
    data = await state.get_data()
    if "wt_name" not in data:
        raise ValidationError("Начните заново: Работы и цены → Добавить работу")
    method = callback.data.split(":")[3]
    if method == "per_unit":
        await state.update_data(wt_method=method)
        units = [(u, f"p:w:u:{u}") for u in COMMON_UNITS]
        grid = [units[i : i + 3] for i in range(0, len(units), 3)]
        grid.append([("Другое…", "p:w:u:*"), ("✖️ Отмена", "p:w")])
        _show(
            callback, notifier, f"{h(data['wt_name'])}: за что платим? Например, за заказ, за штуку, за кг.", _kb(grid)
        )
        return
    await _create_work(
        callback, state, session, actor, settings, notifier, method, "сумма" if method == "percent" else "шт"
    )


@router.callback_query(F.data.startswith("p:w:u:"))
async def work_unit(callback: CallbackQuery, state: FSMContext, session, actor, settings, notifier):
    unit = callback.data.split(":", 3)[3]
    if unit == "*":
        units = [(f"{u.code} ({u.name})", f"p:w:u:{u.code}") for u in await reference.list_units(session)]
        grid = [units[i : i + 2] for i in range(0, len(units), 2)]
        grid.append([("✖️ Отмена", "p:w")])
        _show(callback, notifier, "Выберите единицу:", _kb(grid))
        return
    await _create_work(callback, state, session, actor, settings, notifier, "per_unit", unit)


async def _create_work(callback, state, session, actor, settings, notifier, method: str, unit: str):
    data = await state.get_data()
    if "wt_name" not in data:
        raise ValidationError("Начните заново: Работы и цены → Добавить работу")
    code = await org.unique_code(session, WorkType.code, data["wt_name"])
    wt = await tariffs.create_work_type(session, actor, code, unit, method, data["wt_name"])
    await session.flush()
    await _ask(state, "tariff_rate", wt_id=wt.id, dept_id=None, emp_id=None)
    _show(
        callback,
        notifier,
        "✅ Работа добавлена.\n\n" + _rate_prompt(wt, settings, "для всех"),
        _cancel_kb(f"p:w:v:{wt.id}"),
    )


async def _rate_scope(event, session, wt_id: int, notifier, created: bool = False):
    wt = await session.get(WorkType, wt_id)
    rows = [[("🌐 Для всех", f"p:w:s:{wt.id}:0")]]
    for dept in await org.list_departments(session):
        rows.append([(f"🏢 {dept.name}", f"p:w:s:{wt.id}:{dept.id}")])
    rows.append([("👤 Для сотрудника", f"p:w:e:{wt.id}")])
    rows.append([("⬅️ Назад", f"p:w:v:{wt.id}")])
    _show(event, notifier, f"💰 Цена {h(wt.name)}: для кого?", _kb(rows))


@router.callback_query(F.data.startswith("p:w:t:"))
async def work_rate(callback: CallbackQuery, session, notifier):
    await _rate_scope(callback, session, int(callback.data.split(":")[3]), notifier)


@router.callback_query(F.data.startswith("p:w:e:"))
async def work_rate_employee(callback: CallbackQuery, session, notifier):
    wt_id = int(callback.data.split(":")[3])
    rows = [
        [(f"👤 {e.full_name}", f"p:w:x:{wt_id}:{e.id}")] for e, _ in await org.list_employees(session) if e.is_active
    ]
    rows.append([("⬅️ Назад", f"p:w:v:{wt_id}")])
    _show(callback, notifier, "Кому своя цена?", _kb(rows))


def _rate_prompt(wt: WorkType, settings, scope: str) -> str:
    cur = currency_label(settings.currency)
    who = "" if scope == "для всех" else f" ({h(scope)})"
    if wt.calc_method == "percent":
        what = f"💰 Какой процент от суммы заказа платим{who}? Напишите число, например <b>3.5</b>"
    elif wt.calc_method == "fixed":
        what = f"💰 Сколько платим за один раз{who}? Напишите сумму в {cur}, например <b>700</b>"
    else:
        what = (
            f"💰 Сколько платим за 1 {h(wt.unit_code)}{who}? Напишите сумму в {cur}, например <b>400</b>\n\n"
            f"<i>Если цена растёт с объёмом за полмесяца, например <b>400 50:450</b>: "
            f"первые 50 по {cur}400, дальше по {cur}450</i>"
        )
    return f"{h(wt.name)}\n{what}"


@router.callback_query(F.data.startswith("p:w:s:") | F.data.startswith("p:w:x:"))
async def work_rate_input(callback: CallbackQuery, state: FSMContext, session, settings, notifier):
    parts = callback.data.split(":")
    wt = await session.get(WorkType, int(parts[3]))
    if parts[2] == "s":
        dept_id = int(parts[4]) or None
        emp_id = None
        scope = (await session.get(Department, dept_id)).name if dept_id else "для всех"
    else:
        emp_id = int(parts[4])
        emp = await session.get(Employee, emp_id)
        dept_id = await org.default_department_id(session, emp_id)
        scope = emp.full_name
    await _ask(state, "tariff_rate", wt_id=wt.id, dept_id=dept_id, emp_id=emp_id)
    _show(callback, notifier, _rate_prompt(wt, settings, scope), _cancel_kb(f"p:w:v:{wt.id}"))


async def form_tariff_rate(message, state, session, actor, settings, notifier, data, **_):
    wt = await session.get(WorkType, data["wt_id"])
    items = message.text.replace(",", ".").split() if ":" in message.text else message.text.split()
    rate = parse_decimal(items[0], allow_zero=True, max_scale=6, what="ставку")
    tiers = []
    for item in items[1:]:
        threshold, sep, tier_rate = item.partition(":")
        if not sep:
            raise ValidationError("Ступени пишутся так: 50:450 100:500")
        tiers.append((parse_decimal(threshold, what="порог"), parse_decimal(tier_rate, allow_zero=True, what="ставку")))
    dept = await session.get(Department, data["dept_id"]) if data.get("dept_id") else None
    if dept is None and not actor.can(Perm.TARIFF_MANAGE):
        # Руководитель с правами на свою точку: "для всех" = для его точки.
        dept = await org.single_department(session)
    employees = [await session.get(Employee, data["emp_id"])] if data.get("emp_id") else None
    _, repriced = await work.add_tariffs(
        session,
        actor,
        settings,
        work_type=wt,
        rate=rate,
        currency=None,
        department=dept,
        employees=employees,
        valid_from=None,
        tiers=tiers or None,
    )
    await state.clear()
    await session.flush()
    note = f"\n♻️ Пересчитано неподтверждённых операций: {repriced}" if repriced else ""
    notifier.answer(message, "✅ Цена сохранена." + note)
    notifier.answer(message, *await work_view(session, settings, wt.id))


# ------------------------------------------------------------------ коэффициенты


async def coefs_view(session):
    lines = [
        "⚡ <b>Коэффициенты</b>",
        "Умножают сумму операции. В группе пишутся со звёздочкой: <code>/op ivan доставка 3 *срочно</code>",
        "",
    ]
    rows = []
    for coef in await tariffs.list_coefficients(session):
        state = "" if coef.is_active else " (выключен)"
        lines.append(f"• <b>*{h(coef.code)}</b> ×{fmt_number(coef.factor)}: {h(coef.name)}{state}")
        rows.append([(("⏸ " if coef.is_active else "▶️ ") + f"*{coef.code}", f"p:c:tg:{coef.code}")])
    rows.append([("➕ Новый коэффициент", "p:c:add")])
    rows.append([("⬅️ Назад", "p:more")])
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data == "p:c")
async def coefs(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    _show(callback, notifier, *await coefs_view(session))


@router.callback_query(F.data.startswith("p:c:tg:"))
async def coef_toggle(callback: CallbackQuery, session, actor, notifier):
    code = callback.data.split(":", 3)[3]
    current = next((c for c in await tariffs.list_coefficients(session) if c.code == code), None)
    if current is None:
        raise NotFound("Коэффициент не найден")
    await tariffs.set_coefficient_active(session, actor, code, not current.is_active)
    await session.flush()
    _show(callback, notifier, *await coefs_view(session))


@router.callback_query(F.data == "p:c:add")
async def coef_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    actor.require(Perm.TARIFF_MANAGE)
    await _ask(state, "coef")
    _show(
        callback,
        notifier,
        "✏️ Напишите слово и множитель, например: <b>срочно 1.5</b> или <b>выходной 2</b>",
        _cancel_kb("p:c"),
    )


async def form_coef(message, state, session, actor, notifier, **_):
    items = message.text.split()
    if len(items) < 2:
        raise ValidationError("Нужно слово и множитель: срочно 1.5")
    factor = parse_decimal(items[-1], max_scale=4, what="множитель")
    code = items[0].lower().lstrip("*")
    await tariffs.create_coefficient(session, actor, code, factor, " ".join(items[:-1]))
    await state.clear()
    await session.flush()
    notifier.answer(message, *await coefs_view(session))


# ------------------------------------------------------------------ товары


async def products_view(session):
    lines = ["📦 <b>Товары</b>", ""]
    rows = []
    for product, units in await stock.list_products(session):
        alt = ", ".join(f"1 {u.unit_code} = {fmt_number(u.factor)} {product.base_unit}" for u in units)
        lines.append(f"• <b>{h(product.name)}</b> ({h(product.base_unit)}){' · ' + alt if alt else ''}")
        rows.append([(f"📦 {product.name}", f"p:pr:v:{product.id}")])
    if not rows:
        lines.append("Пока нет товаров.")
    rows.append([("➕ Новый товар", "p:pr:add")])
    rows.append([("⬅️ Назад", "p:more")])
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data == "p:pr")
async def products(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    _show(callback, notifier, *await products_view(session))


async def product_view(session, product_id: int):
    product = await session.get(Product, product_id)
    lines = [
        f"📦 <b>{h(product.name)}</b>  <i>артикул для команд: {h(product.sku)}</i>",
        f"Учёт в: {h(product.base_unit)}",
    ]
    for batch, remaining in (await stock.product_batches(session, product))[:10]:
        cost = rate_text(batch.unit_cost, batch.currency) if batch.unit_cost is not None else "без цены"
        lines.append(f"• партия от {batch.received_on:%d.%m}: осталось {fmt_qty(remaining, product.base_unit)}, {cost}")
    rows = [[("➕ Фасовка", f"p:pr:pk:{product.id}")], [("⬅️ Товары", "p:pr")]]
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data.startswith("p:pr:v:"))
async def product(callback: CallbackQuery, session, notifier):
    _show(callback, notifier, *await product_view(session, int(callback.data.split(":")[3])))


@router.callback_query(F.data == "p:pr:add")
async def product_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    actor.require(Perm.STOCK_MANAGE)
    await _ask(state, "product_name")
    _show(callback, notifier, "✏️ Название товара, например: <b>Кофе зерно</b>", _cancel_kb("p:pr"))


async def form_product_name(message, state, session, notifier, **_):
    name = " ".join(message.text.split())
    await state.update_data(product_name=name)
    units = [(f"{u.code}", f"p:pr:u:{u.code}") for u in await reference.list_units(session)]
    grid = [units[i : i + 3] for i in range(0, len(units), 3)]
    grid.append([("✖️ Отмена", "p:pr")])
    notifier.answer(message, f"{h(name)}: в чём ведём учёт на складе?", _kb(grid))


@router.callback_query(F.data.startswith("p:pr:u:"))
async def product_unit_choice(callback: CallbackQuery, state: FSMContext, session, actor, notifier):
    data = await state.get_data()
    if "product_name" not in data:
        raise ValidationError("Начните заново: Товары → Новый товар")
    unit = callback.data.split(":", 3)[3]
    sku = await org.unique_code(session, Product.sku, data["product_name"])
    created = await stock.create_product(session, actor, sku, unit, data["product_name"])
    await state.clear()
    await session.flush()
    _show(callback, notifier, *await product_view(session, created.id))


@router.callback_query(F.data.startswith("p:pr:pk:"))
async def product_pack(callback: CallbackQuery, session, notifier):
    parts = callback.data.split(":")
    product_id = int(parts[3])
    units = [(u.code, f"p:pr:pu:{product_id}:{u.code}") for u in await reference.list_units(session)]
    grid = [units[i : i + 3] for i in range(0, len(units), 3)]
    grid.append([("⬅️ Назад", f"p:pr:v:{product_id}")])
    _show(callback, notifier, "Как называется фасовка?", _kb(grid))


@router.callback_query(F.data.startswith("p:pr:pu:"))
async def product_pack_unit(callback: CallbackQuery, state: FSMContext, session, notifier):
    _, _, _, product_id, unit = callback.data.split(":", 4)
    product = await session.get(Product, int(product_id))
    await _ask(state, "product_pack", product_id=product.id, unit=unit)
    _show(
        callback,
        notifier,
        f"Сколько {h(product.base_unit)} в 1 {h(unit)}? Например <b>0.25</b>",
        _cancel_kb(f"p:pr:v:{product.id}"),
    )


async def form_product_pack(message, state, session, actor, notifier, data, **_):
    product = await session.get(Product, data["product_id"])
    factor = parse_decimal(message.text, what="количество")
    await stock.add_product_unit(session, actor, product, data["unit"], factor)
    await state.clear()
    await session.flush()
    notifier.answer(message, *await product_view(session, product.id))


# ------------------------------------------------------------------ рабочие группы

GROUPS_HELP = """💬 <b>Группы</b>

<b>Как подключить:</b>
1. Создайте группу в Telegram и добавьте туда людей и этого бота.
2. Бот сам спросит в группе, чья она, нажмите кнопку:
   👥 <b>Общая</b>: работу вносят по любому сотруднику
   👤 <b>Группа сотрудника</b>: личный чат с одним человеком, бот сразу подставляет его

В группах суммы не видны, зарплату каждый смотрит в личке с ботом."""


async def groups_view(session):
    lines = [GROUPS_HELP, "", "<b>Подключены:</b>"]
    rows = []
    multi_dept = len(await org.active_departments(session)) > 1
    for chat, dept, emp in await org.list_chats(session):
        kind = f"группа сотрудника {emp.full_name}" if emp else "общая"
        title = chat.title or str(chat.chat_id)
        where = f"{h(dept.name)}, " if multi_dept else ""
        lines.append(f"• {h(title)}: {where}{h(kind)}")
        rows.append([(f"❌ Отвязать {title[:30]}", f"p:g:x:{chat.chat_id}")])
    if not rows:
        lines.append("пока нет")
    rows.append([("⬅️ В меню", "p:home")])
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data == "p:g")
async def groups(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    _show(callback, notifier, *await groups_view(session))


@router.callback_query(F.data.startswith("p:g:x:"))
async def group_unbind(callback: CallbackQuery, session, actor, notifier):
    chat_id = int(callback.data.split(":")[3])
    await org.unbind_chat(session, actor, chat_id)
    await session.flush()
    notifier.send(chat_id, "Группа отвязана от учёта. Подключить снова: /bind")
    _show(callback, notifier, *await groups_view(session))


async def _bind_departments_kb(session) -> InlineKeyboardMarkup:
    rows = [[(f"🏢 {d.name}", f"g:b:{d.id}")] for d in await org.list_departments(session) if d.is_active]
    if not rows:
        rows = [[("Сначала настройте бота в личке: /admin", "g:none")]]
    return _kb(rows)


async def _kind_kb(session, dept: Department) -> InlineKeyboardMarkup:
    rows = [[("👥 Общая группа", f"g:o:{dept.id}")]]
    for emp in await org.department_employees(session, dept.id):
        rows.append([(f"👤 {emp.full_name}", f"g:e:{dept.id}:{emp.id}")])
    return _kb(rows)


async def _ask_bind(chat_id: int, session, notifier) -> None:
    single = await org.single_department(session)
    if single is not None:
        notifier.send(chat_id, "👋 Привет! Чья это группа?", await _kind_kb(session, single))
        return
    notifier.send(chat_id, "👋 Привет! К какому филиалу относится эта группа?", await _bind_departments_kb(session))


@router.message(Command("bind"))
async def bind(message: Message, session, actor, notifier, is_private):
    """Запасной способ подключить группу, если бот не спросил об этом сам при входе."""
    if is_private:
        raise ValidationError("Эту команду нужно написать в рабочей группе")
    if not actor.can_anywhere(Perm.ORG_MANAGE):
        raise AccessDenied("Подключить группу может владелец или администратор")
    await _ask_bind(message.chat.id, session, notifier)


def _bound_text(dept: Department, emp: Employee | None) -> str:
    if emp:
        return (
            f"✅ Это группа сотрудника <b>{h(emp.full_name)}</b>.\n"
            "Внести работу: /op, выбрать работу и количество. Или одной строкой: <code>/op доставка 3</code>"
        )
    return (
        "✅ Группа подключена.\n"
        "Внести работу: /op, выбрать сотрудника, работу и количество. "
        "Или одной строкой: <code>/op иван доставка 3</code>"
    )


@router.callback_query(F.data.startswith("g:b:"))
async def bind_department(callback: CallbackQuery, session, actor, notifier):
    dept = await session.get(Department, int(callback.data.split(":")[2]))
    actor.require(Perm.ORG_MANAGE, dept.id)
    _show(callback, notifier, f"🏢 {h(dept.name)}. Чья это группа?", await _kind_kb(session, dept))


@router.callback_query(F.data.startswith("g:o:") | F.data.startswith("g:e:"))
async def bind_finish(callback: CallbackQuery, session, actor, notifier):
    parts = callback.data.split(":")
    dept = await session.get(Department, int(parts[2]))
    emp = await session.get(Employee, int(parts[3])) if parts[1] == "e" else None
    await org.bind_chat(session, actor, callback.message.chat.id, callback.message.chat.title or "", dept, emp)
    _show(callback, notifier, _bound_text(dept, emp))


@router.my_chat_member()
async def bot_added(event: ChatMemberUpdated, session, actor, notifier):
    """Бота добавили в группу, сразу спрашиваем, чья это группа."""
    if event.chat.type == "private":
        return
    was_in = event.old_chat_member.status in ("member", "administrator", "creator")
    now_in = event.new_chat_member.status in ("member", "administrator", "creator")
    if was_in or not now_in:
        return
    dept, _ = await org.chat_binding(session, event.chat.id)
    if dept is not None:
        notifier.send(event.chat.id, f"Я снова здесь. Группа подключена к {h(dept.name)}.")
        return
    if actor is not None and actor.can_anywhere(Perm.ORG_MANAGE):
        await _ask_bind(event.chat.id, session, notifier)
    else:
        notifier.send(event.chat.id, "👋 Чтобы я начал работать, администратор должен написать здесь /bind.")


# ------------------------------------------------------------------ настройки


async def settings_view(settings):
    tz_title = next((t for t, z in TIMEZONES if z == settings.timezone), settings.timezone)
    limit = (
        f"{fmt_number(settings.deduction_limit_percent)} % от начислений периода"
        if settings.deduction_limit_percent is not None
        else "нет"
    )
    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        f"💱 Валюта: <b>{currency_label(settings.currency)} ({settings.currency})</b>",
        f"🕐 Часовой пояс: <b>{h(tz_title)}</b>",
        f"📅 Расчётные периоды: <b>1-{settings.payroll_split_day} и {settings.payroll_split_day + 1}-конец месяца</b>",
        f"🔢 Округление: <b>{ROUNDING_TITLES.get(settings.rounding_mode, settings.rounding_mode)}</b>",
        f"📉 Лимит удержаний: <b>{limit}</b>",
        f"💳 Аванс в долг по умолчанию: <b>{fmt_money(settings.default_credit_limit, settings.currency) if settings.default_credit_limit else 'нельзя'}</b>",
    ]
    for key, title in TOGGLES.items():
        lines.append(f"{title}: <b>{'да' if getattr(settings, key) else 'нет'}</b>")
    rows = [
        [("💱 Валюта", "p:s:cur"), ("🕐 Часовой пояс", "p:s:tz")],
        [("📅 Периоды", "p:s:split"), ("🔢 Округление", "p:s:rnd")],
        [("📉 Лимит удержаний", "p:s:lim"), ("💳 Аванс по умолчанию", "p:s:cr")],
    ]
    for key, title in TOGGLES.items():
        rows.append([(f"{'✅' if getattr(settings, key) else '⬜️'} {title[2:]}", f"p:s:t:{key}")])
    rows.append([("⬅️ Назад", "p:more")])
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data == "p:s")
async def settings_section(callback: CallbackQuery, state: FSMContext, settings, notifier):
    await state.clear()
    _show(callback, notifier, *await settings_view(settings))


async def _set(session, actor, key: str, value: str):
    await settings_service.set_setting(session, actor, key, value)
    await session.flush()
    return await settings_service.get_settings(session)


@router.callback_query(F.data.startswith("p:s:t:"))
async def settings_toggle(callback: CallbackQuery, session, actor, settings, notifier):
    key = callback.data.split(":")[3]
    new = await _set(session, actor, key, "false" if getattr(settings, key) else "true")
    _show(callback, notifier, *await settings_view(new))


@router.callback_query(F.data == "p:s:cur")
async def settings_currency(callback: CallbackQuery, session, settings, notifier):
    codes = sorted(await reference.currency_codes(session))
    buttons = [
        (
            ("✅ " if c == settings.currency else "") + (f"{currency_label(c)} {c}" if currency_label(c) != c else c),
            f"p:s:cur:{c}",
        )
        for c in codes
    ]
    grid = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    grid.append([("⬅️ Назад", "p:s")])
    _show(callback, notifier, "💱 Валюта для новых ставок и выплат. Уже внесённые суммы не меняются.", _kb(grid))


@router.callback_query(F.data.startswith("p:s:cur:"))
async def settings_currency_set(callback: CallbackQuery, session, actor, notifier):
    new = await _set(session, actor, "currency", callback.data.split(":")[3])
    _show(callback, notifier, *await settings_view(new))


@router.callback_query(F.data == "p:s:tz")
async def settings_tz(callback: CallbackQuery, settings, notifier):
    buttons = [
        (("✅ " if z == settings.timezone else "") + title, f"p:s:tz:{i}") for i, (title, z) in enumerate(TIMEZONES)
    ]
    grid = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    grid.append([("✏️ Другой", "p:s:in:timezone"), ("⬅️ Назад", "p:s")])
    _show(callback, notifier, "🕐 Часовой пояс: по нему считаются сегодня и границы периодов.", _kb(grid))


@router.callback_query(F.data.startswith("p:s:tz:"))
async def settings_tz_set(callback: CallbackQuery, session, actor, notifier):
    new = await _set(session, actor, "timezone", TIMEZONES[int(callback.data.split(":")[3])][1])
    _show(callback, notifier, *await settings_view(new))


@router.callback_query(F.data == "p:s:split")
async def settings_split(callback: CallbackQuery, notifier):
    grid = [[(f"1-{d} и {d + 1}-конец", f"p:s:v:payroll_split_day:{d}")] for d in (10, 14, 15, 20)]
    grid.append([("✏️ Другой день", "p:s:in:payroll_split_day"), ("⬅️ Назад", "p:s")])
    _show(callback, notifier, "📅 Выплаты два раза в месяц: где граница первой половины?", _kb(grid))


@router.callback_query(F.data == "p:s:rnd")
async def settings_rounding(callback: CallbackQuery, notifier):
    grid = [[(title, f"p:s:v:rounding_mode:{code}")] for code, title in ROUNDING_TITLES.items()]
    grid.append([("⬅️ Назад", "p:s")])
    _show(callback, notifier, "🔢 Как округлять суммы до центов?", _kb(grid))


@router.callback_query(F.data == "p:s:lim")
async def settings_limit(callback: CallbackQuery, notifier):
    grid = [[(f"{p} %", f"p:s:v:deduction_limit_percent:{p}") for p in (10, 20, 30, 50)]]
    grid.append([("Без лимита", "p:s:v:deduction_limit_percent:-"), ("✏️ Другой", "p:s:in:deduction_limit_percent")])
    grid.append([("⬅️ Назад", "p:s")])
    _show(
        callback,
        notifier,
        "📉 Сколько процентов от начислений периода можно удержать штрафами/удержаниями? "
        "Остальное удержится из следующих начислений.",
        _kb(grid),
    )


@router.callback_query(F.data == "p:s:cr")
async def settings_credit(callback: CallbackQuery, state: FSMContext, notifier):
    await _ask(state, "setting", key="default_credit_limit")
    _show(
        callback,
        notifier,
        "💳 Сколько по умолчанию можно выдать сверх заработанного? Напишите число (0 значит нельзя).\n"
        "Каждому сотруднику можно задать своё в его карточке.",
        _cancel_kb("p:s"),
    )


@router.callback_query(F.data.startswith("p:s:v:"))
async def settings_value(callback: CallbackQuery, session, actor, notifier):
    _, _, _, key, value = callback.data.split(":", 4)
    new = await _set(session, actor, key, value)
    _show(callback, notifier, *await settings_view(new))


@router.callback_query(F.data.startswith("p:s:in:"))
async def settings_input(callback: CallbackQuery, state: FSMContext, notifier):
    key = callback.data.split(":")[3]
    await _ask(state, "setting", key=key)
    _show(callback, notifier, f"✏️ {h(SPECS[key].title)}\nНапишите значение:", _cancel_kb("p:s"))


async def form_setting(message, state, session, actor, notifier, data, **_):
    new = await _set(session, actor, data["key"], message.text.strip())
    await state.clear()
    notifier.answer(message, *await settings_view(new))


# ------------------------------------------------------------------ журнал


@router.callback_query(F.data == "p:j")
async def journal(callback: CallbackQuery, session, actor, notifier):
    if not (actor.is_owner or actor.can(Perm.SETTINGS_MANAGE)):
        raise AccessDenied("Журнал доступен владельцу и администратору")
    rows = (
        await session.execute(
            select(AuditLog, User)
            .outerjoin(User, User.id == AuditLog.actor_user_id)
            .order_by(AuditLog.id.desc())
            .limit(20)
        )
    ).all()
    lines = ["📜 <b>Последние изменения</b>", ""]
    for entry, user in rows:
        who = (user.full_name or f"@{user.username}") if user else "система"
        what = ACTION_TITLES.get(entry.action, entry.action)
        lines.append(f"{entry.at.astimezone(dt.UTC):%d.%m %H:%M} · {h(who)} · {h(what)} #{h(entry.entity_id or '')}")
    _show(callback, notifier, "\n".join(lines), _kb([[("⬅️ Назад", "p:more")]]))


# ------------------------------------------------------------------ ввод ответов на вопросы панели

FORMS = {
    "dept_name": form_dept_name,
    "emp_name": form_emp_name,
    "emp_credit": form_emp_credit,
    "emp_position": form_emp_position,
    "grant_user": form_grant_user,
    "wt_name": form_wt_name,
    "tariff_rate": form_tariff_rate,
    "coef": form_coef,
    "product_name": form_product_name,
    "product_pack": form_product_pack,
    "setting": form_setting,
}


@router.message(AdminForm.input, F.text & ~F.text.startswith("/"))
async def form_input(message: Message, state: FSMContext, session, actor, settings, notifier, bot: Bot):
    data = await state.get_data()
    handler = FORMS.get(data.get("form"))
    if handler is None:
        await state.clear()
        return
    await handler(
        message, state=state, session=session, actor=actor, settings=settings, notifier=notifier, data=data, bot=bot
    )


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
            role_name = (await _role_names(session)).get(invite.role_code, invite.role_code)
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
