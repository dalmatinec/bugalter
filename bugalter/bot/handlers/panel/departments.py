"""Филиалы. Если точка одна, раздел не нужен: подразделение по умолчанию создаётся само."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from ....db.models import Department
from ....domain.errors import NotFound
from ....domain.permissions import Perm
from ....services import org
from ...ui import h
from .base import ask, cancel_kb, form, kb, show

router = Router(name="panel.departments")


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
    return "\n".join(lines), kb(rows)


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
    markup = kb(
        [
            [("➕ Новый сотрудник", f"p:e:add:{dept.id}")],
            [("💬 Подключить группу", "p:g")],
            [("⬅️ Филиалы", "p:d")],
        ]
    )
    return "\n".join(lines), markup


async def dept_choice(session, prefix: str, exclude: set[int] | None = None, only: set[int] | None = None):
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
    return kb(rows)


@router.callback_query(F.data == "p:d")
async def departments(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    show(callback, notifier, *await departments_view(session))


@router.callback_query(F.data.startswith("p:d:v:"))
async def department(callback: CallbackQuery, session, notifier):
    show(callback, notifier, *await department_view(session, int(callback.data.split(":")[3])))


@router.callback_query(F.data == "p:d:add")
async def department_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    actor.require(Perm.ORG_MANAGE)
    await ask(state, "dept_name")
    show(
        callback,
        notifier,
        "✏️ Напишите название филиала, например: <b>Север</b> или <b>Склад на Ленина</b>",
        cancel_kb("p:d"),
    )


@form("dept_name")
async def form_dept_name(message, state, session, actor, notifier, data, **_):
    name = message.text.strip()
    code = await org.unique_code(session, Department.code, name)
    dept = await org.create_department(session, actor, code, name)
    await state.clear()
    await session.flush()
    notifier.answer(message, f"✅ Филиал {h(dept.name)} создан.", None)
    notifier.answer(message, *await department_view(session, dept.id))
