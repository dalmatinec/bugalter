"""Рабочие группы: подключение группы (общая или группа сотрудника) и список подключённых."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup, Message

from ....db.models import Department, Employee
from ....domain.errors import AccessDenied, ValidationError
from ....domain.permissions import Perm
from ....services import org
from ...ui import h
from .base import kb, show

router = Router(name="panel.groups")

GROUPS_HELP = """💬 <b>Группы</b>

<b>Как подключить:</b>
1. Создайте группу в Telegram (с любого аккаунта) и добавьте туда сотрудника, при желании оператора, и этого бота.
2. Сделайте бота <b>администратором</b> группы, иначе он не видит обычные сообщения с тегами.
3. Бот сам спросит, чья это группа. Выберите сотрудника.

В группе сотрудника всё, что написано тегами, записывается на него:
<code>#доставка 3</code> или несколько сразу: <code>#фасовка05 20 #фасовка1 5</code>
Или столбиком: название, под ним количество х вес (вес как в названии работы):
<code>Готовые крам
5х05
10х1</code>
Это 5 шт работы Готовые крам 0.5 и 10 шт работы Готовые крам 1.
Или одной строкой: название работы и сколько штук: <code>Крам факт 1 13</code> (без числа значит 1 шт).
Бот считает по ценам из Работ и цен и присылает карточку, оператор или владелец подтверждает кнопкой.

👥 <b>Общая группа</b> тоже бывает: там тегами отчитывается сам сотрудник, работа записывается на него.
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
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "p:g")
async def groups(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    show(callback, notifier, *await groups_view(session))


@router.callback_query(F.data.startswith("p:g:x:"))
async def group_unbind(callback: CallbackQuery, session, actor, notifier):
    chat_id = int(callback.data.split(":")[3])
    await org.unbind_chat(session, actor, chat_id)
    await session.flush()
    notifier.send(chat_id, "Группа отвязана от учёта. Подключить снова: /bind")
    show(callback, notifier, *await groups_view(session))


async def _bind_departments_kb(session) -> InlineKeyboardMarkup:
    rows = [[(f"🏢 {d.name}", f"g:b:{d.id}")] for d in await org.list_departments(session) if d.is_active]
    if not rows:
        rows = [[("Сначала настройте бота в личке: /admin", "g:none")]]
    return kb(rows)


async def _kind_kb(session, dept: Department) -> InlineKeyboardMarkup:
    rows = [[("👥 Общая группа", f"g:o:{dept.id}")]]
    for emp in await org.department_employees(session, dept.id):
        rows.append([(f"👤 {emp.full_name}", f"g:e:{dept.id}:{emp.id}")])
    return kb(rows)


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


ADMIN_HINT = "\n\n⚠️ Сделайте меня администратором группы, иначе я не увижу сообщения с тегами."


def _bound_text(dept: Department, emp: Employee | None) -> str:
    if emp:
        return (
            f"✅ Это группа: <b>{h(emp.full_name)}</b>.\n"
            "Отчёт о работе пишите тегом и количеством: <code>#доставка 3</code>, можно несколько в одном "
            "сообщении. Теги работ видны у владельца в Работах и ценах. Или кнопками: /op" + ADMIN_HINT
        )
    return (
        "✅ Группа подключена.\n"
        "Сотрудник отчитывается тегами: <code>#доставка 3</code>, работа записывается на того, кто написал. "
        "Оператор вносит за другого кнопками: /op" + ADMIN_HINT
    )


@router.callback_query(F.data.startswith("g:b:"))
async def bind_department(callback: CallbackQuery, session, actor, notifier):
    dept = await session.get(Department, int(callback.data.split(":")[2]))
    actor.require(Perm.ORG_MANAGE, dept.id)
    show(callback, notifier, f"🏢 {h(dept.name)}. Чья это группа?", await _kind_kb(session, dept))


@router.callback_query(F.data.startswith("g:o:") | F.data.startswith("g:e:"))
async def bind_finish(callback: CallbackQuery, session, actor, notifier):
    parts = callback.data.split(":")
    dept = await session.get(Department, int(parts[2]))
    emp = await session.get(Employee, int(parts[3])) if parts[1] == "e" else None
    await org.bind_chat(session, actor, callback.message.chat.id, callback.message.chat.title or "", dept, emp)
    show(callback, notifier, _bound_text(dept, emp))


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
