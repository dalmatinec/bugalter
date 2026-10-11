"""Администрирование: подразделения, сотрудники, роли, группы, справочники, виды работ, тарифы, настройки."""

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select

from ...db.models import AuditLog, Role
from ...domain.dates import parse_date
from ...domain.errors import AccessDenied, ValidationError
from ...domain.money import fmt_number, parse_decimal
from ...domain.permissions import PERM_TITLES, Perm
from ...services import access, org, reference, tariffs, work
from ...services import settings as settings_service
from ...services.settings import SPECS
from ..args import extract, need, parse_id, tokens
from ..ui import h

router = Router(name="admin")

ADMIN_HELP = """<b>Администрирование</b>
<u>Структура</u>
/dept_add &lt;код&gt; &lt;название&gt; · /depts
/emp_add &lt;код&gt; &lt;ФИО&gt; · /emps · /emp_link &lt;код&gt; &lt;tg_id|@username&gt;
/emp_dept &lt;код&gt; &lt;подразделение&gt; · /emp_undept &lt;код&gt; &lt;подразделение&gt; · /emp_off · /emp_on
/bind &lt;подразделение&gt; — привязать рабочую группу (команда в группе) · /unbind
<u>Доступ</u>
/roles · /role_add &lt;код&gt; &lt;право,право&gt; &lt;название&gt; (владелец) · /perms
/grant &lt;tg_id|@username&gt; &lt;роль&gt; [подразделение|*] · /grants [tg_id|@username] · /revoke &lt;№&gt;
<u>Работы и тарифы</u>
/wt_add &lt;код&gt; &lt;единица&gt; &lt;per_unit|fixed&gt; &lt;название&gt; · /wt · /wt_off &lt;код&gt; · /wt_on &lt;код&gt;
/wt_rename &lt;код&gt; &lt;название&gt; · /wt_depts &lt;код&gt; &lt;подр,подр|*&gt; · /wt_round &lt;код&gt; &lt;шаг|-&gt; [режим]
/tariff &lt;работа&gt; &lt;ставка&gt; [USD] [dept=код] [emp=код,код] [from=ДД.ММ.ГГГГ] [комментарий]
/tariffs &lt;работа&gt; — история ставок
<u>Справочники и настройки</u>
/units · /unit_add &lt;код&gt; &lt;знаков&gt; [название] · /currency_add &lt;код&gt; &lt;знаков&gt; &lt;название&gt;
/products · /product_add &lt;артикул&gt; &lt;единица&gt; &lt;название&gt; · /product_unit &lt;артикул&gt; &lt;единица&gt; &lt;коэфф.&gt;
/settings · /set &lt;ключ&gt; &lt;значение&gt; · /audit [N]"""


def _private(is_private: bool) -> None:
    if not is_private:
        raise ValidationError("Эта команда выполняется только в личном чате с ботом")


@router.message(Command("admin"))
async def admin_help(message: Message, notifier):
    notifier.answer(message, ADMIN_HELP)


# ------------------------------------------------------------------ структура


@router.message(Command("dept_add"))
async def dept_add(message: Message, command: CommandObject, session, actor, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 2, "/dept_add <код> <название>")
    dept = await org.create_department(session, actor, items[0], " ".join(items[1:]))
    notifier.answer(message, f"🏢 Подразделение <b>{h(dept.name)}</b> (код {h(dept.code)}) создано.")


@router.message(Command("depts"))
async def depts(message: Message, session, notifier):
    rows = await org.list_departments(session)
    text = "\n".join(f"• <b>{h(d.code)}</b> — {h(d.name)}{'' if d.is_active else ' (неактивно)'}" for d in rows)
    notifier.answer(message, text or "Подразделений нет. /dept_add <код> <название>")


@router.message(Command("emp_add"))
async def emp_add(message: Message, command: CommandObject, session, actor, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 2, "/emp_add <код> <ФИО>")
    emp = await org.create_employee(session, actor, items[0], " ".join(items[1:]))
    notifier.answer(
        message,
        f"👤 Сотрудник <b>{h(emp.full_name)}</b>, код <code>{h(emp.code)}</code>.\n"
        f"Дальше: /emp_dept {h(emp.code)} &lt;подразделение&gt; и /emp_link {h(emp.code)} &lt;tg_id&gt;",
    )


@router.message(Command("emps"))
async def emps(message: Message, session, actor, notifier):
    if not (actor.can_anywhere(Perm.ORG_MANAGE) or actor.can_anywhere(Perm.WORK_CREATE)):
        raise AccessDenied("Недостаточно прав")
    rows = await org.list_employees(session)
    lines = []
    for emp, depts_ in rows:
        mark = "" if emp.is_active else " (уволен)"
        tg = "🔗" if emp.user_id else "—"
        lines.append(f"• <code>{h(emp.code)}</code> {h(emp.full_name)}{mark} · {', '.join(depts_) or 'без подр.'} {tg}")
    notifier.answer(message, "\n".join(lines) or "Сотрудников нет. /emp_add <код> <ФИО>")


@router.message(Command("emp_link"))
async def emp_link(message: Message, command: CommandObject, session, actor, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 2, "/emp_link <код> <tg_id|@username>")
    emp = await org.get_employee(session, items[0])
    user = await access.find_user(session, items[1])
    await org.link_user(session, actor, emp, user)
    notifier.answer(message, f"🔗 {h(emp.full_name)} привязан к Telegram {user.telegram_id}.")
    notifier.send(user.telegram_id, f"Вы привязаны к карточке сотрудника <b>{h(emp.full_name)}</b>. Меню: /start")


@router.message(Command("emp_dept", "emp_undept"))
async def emp_dept(message: Message, command: CommandObject, session, actor, notifier):
    items = tokens(command.args)
    need(items, 2, f"/{command.command} <код сотрудника> <подразделение>")
    emp = await org.get_employee(session, items[0])
    dept = await org.get_department(session, items[1])
    if command.command == "emp_dept":
        await org.attach_to_department(session, actor, emp, dept)
        notifier.answer(message, f"✅ {h(emp.full_name)} → {h(dept.code)}")
    else:
        await org.detach_from_department(session, actor, emp, dept)
        notifier.answer(message, f"✅ {h(emp.full_name)} больше не в {h(dept.code)}")


@router.message(Command("emp_off", "emp_on"))
async def emp_active(message: Message, command: CommandObject, session, actor, notifier):
    items = tokens(command.args)
    need(items, 1, f"/{command.command} <код>")
    emp = await org.get_employee(session, items[0])
    await org.set_employee_active(session, actor, emp, command.command == "emp_on")
    notifier.answer(message, f"✅ {h(emp.full_name)}: {'активен' if emp.is_active else 'уволен (история сохранена)'}")


@router.message(Command("bind"))
async def bind(message: Message, command: CommandObject, session, actor, notifier, is_private):
    if is_private:
        raise ValidationError("Команду /bind нужно выполнить в рабочей группе")
    items = tokens(command.args)
    need(items, 1, "/bind <код подразделения>")
    dept = await org.get_department(session, items[0])
    await org.bind_chat(session, actor, message.chat.id, message.chat.title or "", dept)
    notifier.answer(
        message,
        f"✅ Группа привязана к подразделению <b>{h(dept.name)}</b>. "
        "Здесь вводятся операции и складские движения; суммы и зарплаты — только в личке с ботом.",
    )


@router.message(Command("unbind"))
async def unbind(message: Message, session, actor, notifier):
    await org.unbind_chat(session, actor, message.chat.id)
    notifier.answer(message, "Группа отвязана от подразделения.")


# ------------------------------------------------------------------ доступ


@router.message(Command("roles"))
async def roles(message: Message, session, notifier):
    rows = (await session.execute(select(Role).order_by(Role.code))).scalars()
    lines = []
    for role in rows:
        lines.append(f"• <b>{h(role.code)}</b> — {h(role.name)}: {', '.join(role.permissions)}")
    notifier.answer(message, "\n".join(lines) + "\n\nСписок прав: /perms")


@router.message(Command("perms"))
async def perms(message: Message, notifier):
    notifier.answer(message, "\n".join(f"• <code>{p}</code> — {h(t)}" for p, t in PERM_TITLES.items()))


@router.message(Command("role_add"))
async def role_add(message: Message, command: CommandObject, session, actor, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 3, "/role_add <код> <право,право> <название>")
    perms_list = [p.strip() for p in items[1].split(",") if p.strip()]
    role = await access.create_role(session, actor, items[0], " ".join(items[2:]), perms_list)
    notifier.answer(message, f"✅ Роль <b>{h(role.code)}</b>: {', '.join(role.permissions)}")


@router.message(Command("grant"))
async def grant(message: Message, command: CommandObject, session, actor, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 2, "/grant <tg_id|@username> <роль> [подразделение|*]")
    user = await access.find_user(session, items[0])
    dept = None
    if len(items) > 2 and items[2] not in ("*", "all", "все"):
        dept = await org.get_department(session, items[2])
    g = await access.grant_role(session, actor, user, items[1].lower(), dept)
    scope = dept.code if dept else "все подразделения"
    notifier.answer(message, f"✅ Выдача #{g.id}: {h(items[1])} → {user.telegram_id} ({h(scope)})")
    notifier.send(user.telegram_id, f"Вам выдана роль <b>{h(items[1])}</b> ({h(scope)}). Справка: /help")


@router.message(Command("revoke"))
async def revoke(message: Message, command: CommandObject, session, actor, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 1, "/revoke <№ выдачи из /grants>")
    g = await access.revoke_grant(session, actor, parse_id(items[0]))
    notifier.answer(message, f"✅ Выдача #{g.id} ({h(g.role_code)}) отозвана.")


@router.message(Command("grants"))
async def grants(message: Message, command: CommandObject, session, actor, notifier):
    if not actor.can_anywhere(Perm.ACCESS_MANAGE):
        raise AccessDenied("Недостаточно прав")
    items = tokens(command.args)
    user_id = (await access.find_user(session, items[0])).id if items else None
    rows = await access.list_grants(session, user_id)
    lines = [
        f"#{g.id} · {h(u.full_name or str(u.telegram_id))} ({u.telegram_id}) · <b>{h(g.role_code)}</b> · "
        f"{h(d.code) if d else 'все'}"
        for g, u, d in rows
    ]
    notifier.answer(message, "\n".join(lines) or "Выдач нет")


# ------------------------------------------------------------------ справочники


@router.message(Command("units"))
async def units(message: Message, session, notifier):
    rows = await reference.list_units(session)
    notifier.answer(message, "\n".join(f"• <b>{h(u.code)}</b> — {h(u.name)}, знаков: {u.scale}" for u in rows))


@router.message(Command("unit_add"))
async def unit_add(message: Message, command: CommandObject, session, actor, notifier):
    items = tokens(command.args)
    need(items, 2, "/unit_add <код> <знаков после запятой> [название]")
    if not items[1].isdigit():
        raise ValidationError("Число знаков — целое от 0 до 6")
    unit = await reference.add_unit(session, actor, items[0], int(items[1]), " ".join(items[2:]))
    notifier.answer(message, f"✅ Единица {h(unit.code)} (знаков: {unit.scale})")


@router.message(Command("currency_add"))
async def currency_add(message: Message, command: CommandObject, session, actor, notifier):
    items = tokens(command.args)
    need(items, 3, "/currency_add <код> <знаков> <название>")
    if not items[1].isdigit():
        raise ValidationError("Число знаков — целое от 0 до 4")
    cur = await reference.add_currency(session, actor, items[0], int(items[1]), " ".join(items[2:]))
    notifier.answer(message, f"✅ Валюта {cur.code}")


@router.message(Command("settings"))
async def show_settings(message: Message, settings, notifier):
    lines = ["<b>Настройки</b> (изменить: /set &lt;ключ&gt; &lt;значение&gt;)"]
    for key, spec in SPECS.items():
        value = getattr(settings, key)
        lines.append(f"• <code>{key}</code> = <b>{h(str(value))}</b> — {h(spec.title)}")
    notifier.answer(message, "\n".join(lines))


@router.message(Command("set"))
async def set_cmd(message: Message, command: CommandObject, session, actor, notifier, is_private):
    _private(is_private)
    items = tokens(command.args)
    need(items, 2, "/set <ключ> <значение>")
    stored = await settings_service.set_setting(session, actor, items[0], " ".join(items[1:]))
    notifier.answer(message, f"✅ {h(items[0])} = {h(stored)}")


@router.message(Command("audit"))
async def audit_log(message: Message, command: CommandObject, session, actor, notifier, is_private):
    _private(is_private)
    if not (actor.is_owner or actor.can(Perm.SETTINGS_MANAGE)):
        raise AccessDenied("Журнал изменений доступен владельцу и администратору")
    items = tokens(command.args)
    limit = min(int(items[0]), 100) if items and items[0].isdigit() else 20
    rows = (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))).scalars()
    lines = []
    for row in rows:
        lines.append(
            f"{row.at:%d.%m %H:%M} · {h(row.action)} · {h(row.entity_type)}#{h(row.entity_id or '')} · "
            f"user {row.actor_user_id or '—'}"
        )
    notifier.answer(message, "\n".join(lines) or "Журнал пуст")


# ------------------------------------------------------------------ виды работ и тарифы


@router.message(Command("wt_add"))
async def wt_add(message: Message, command: CommandObject, session, actor, notifier):
    items = tokens(command.args)
    need(items, 4, "/wt_add <код> <единица> <per_unit|fixed> <название>")
    wt = await tariffs.create_work_type(session, actor, items[0], items[1], items[2], " ".join(items[3:]))
    notifier.answer(
        message,
        f"✅ Вид работ <b>{h(wt.name)}</b> (код {h(wt.code)}, ед. {h(wt.unit_code)}, "
        f"{tariffs.CALC_METHODS[wt.calc_method]}). Теперь задайте ставку: /tariff {h(wt.code)} &lt;ставка&gt;",
    )


@router.message(Command("wt"))
async def wt_list(message: Message, session, notifier):
    rows = await tariffs.list_work_types(session)
    lines = [
        f"• <code>{h(w.code)}</code> {h(w.name)} · {h(w.unit_code)} · {w.calc_method}"
        + ("" if w.is_active else " (отключён)")
        + ("" if w.all_departments else " · не во всех подр.")
        for w in rows
    ]
    notifier.answer(message, "\n".join(lines) or "Видов работ нет. /wt_add")


@router.message(Command("wt_off", "wt_on"))
async def wt_active(message: Message, command: CommandObject, session, actor, notifier):
    items = tokens(command.args)
    need(items, 1, f"/{command.command} <код>")
    wt = await tariffs.get_work_type(session, items[0])
    await tariffs.set_work_type_active(session, actor, wt, command.command == "wt_on")
    notifier.answer(message, f"✅ {h(wt.name)}: {'включён' if wt.is_active else 'отключён (история сохранена)'}")


@router.message(Command("wt_rename"))
async def wt_rename(message: Message, command: CommandObject, session, actor, notifier):
    items = tokens(command.args)
    need(items, 2, "/wt_rename <код> <новое название>")
    wt = await tariffs.get_work_type(session, items[0])
    await tariffs.rename_work_type(session, actor, wt, " ".join(items[1:]))
    notifier.answer(message, f"✅ {h(wt.code)}: {h(wt.name)}")


@router.message(Command("wt_depts"))
async def wt_depts(message: Message, command: CommandObject, session, actor, notifier):
    items = tokens(command.args)
    need(items, 2, "/wt_depts <код> <подр,подр|*>")
    wt = await tariffs.get_work_type(session, items[0])
    if items[1] in ("*", "все", "all"):
        depts_ = None
    else:
        depts_ = [await org.get_department(session, code) for code in items[1].split(",") if code]
    await tariffs.set_work_type_departments(session, actor, wt, depts_)
    scope = "все подразделения" if depts_ is None else ", ".join(d.code for d in depts_)
    notifier.answer(message, f"✅ {h(wt.name)}: {h(scope)}")


@router.message(Command("wt_round"))
async def wt_round(message: Message, command: CommandObject, session, actor, notifier):
    items = tokens(command.args)
    need(items, 2, "/wt_round <код> <шаг|-> [HALF_UP|HALF_EVEN|DOWN|UP]")
    wt = await tariffs.get_work_type(session, items[0])
    step = None if items[1] == "-" else parse_decimal(items[1], what="шаг округления")
    mode = items[2] if len(items) > 2 else None
    await tariffs.set_work_type_rounding(session, actor, wt, step, mode)
    notifier.answer(message, f"✅ {h(wt.name)}: шаг {step or 'по валюте'}, режим {mode or 'по настройкам'}")


@router.message(Command("tariff"))
async def tariff(message: Message, command: CommandObject, session, actor, settings, notifier, is_private):
    _private(is_private)
    ex = extract(tokens(command.args), ("dept", "emp", "from"))
    need(ex.rest, 2, "/tariff <работа> <ставка> [USD] [dept=код] [emp=код,код] [from=ДД.ММ.ГГГГ] [комментарий]")
    wt = await tariffs.get_work_type(session, ex.rest[0])
    rate = parse_decimal(ex.rest[1], allow_zero=True, max_scale=6, what="ставку")
    rest = ex.rest[2:]
    currency = None
    if rest and rest[0].upper() in await reference.currency_codes(session):
        currency = rest.pop(0).upper()
    dept_code = ex.options.get("dept") or ex.dept
    dept = await org.get_department(session, dept_code) if dept_code else None
    employees = None
    if ex.options.get("emp"):
        employees = [await org.get_employee(session, code) for code in ex.options["emp"].split(",") if code]
    valid_from = parse_date(ex.options["from"], settings.today()) if ex.options.get("from") else None
    created, repriced = await work.add_tariffs(
        session,
        actor,
        settings,
        work_type=wt,
        rate=rate,
        currency=currency,
        department=dept,
        employees=employees,
        valid_from=valid_from,
        comment=" ".join(rest) or None,
    )
    scope = []
    if dept:
        scope.append(f"подразделение {dept.code}")
    if employees:
        scope.append("сотрудники " + ", ".join(e.code for e in employees))
    t = created[0]
    text = (
        f"✅ Тариф «{h(wt.name)}»: {fmt_number(rate)} {t.currency} за {h(wt.unit_code)} "
        f"с {t.valid_from:%d.%m.%Y} ({h('; '.join(scope) or 'общий')}). Записей: {len(created)}."
    )
    if repriced:
        text += f"\n♻️ Пересчитано неподтверждённых операций: {repriced}."
    notifier.answer(message, text)


@router.message(Command("tariffs"))
async def tariff_list(message: Message, command: CommandObject, session, actor, notifier, is_private):
    _private(is_private)
    if not (actor.can_anywhere(Perm.TARIFF_MANAGE) or actor.can_anywhere(Perm.REPORT_VIEW)):
        raise AccessDenied("Недостаточно прав")
    items = tokens(command.args)
    need(items, 1, "/tariffs <работа>")
    wt = await tariffs.get_work_type(session, items[0])
    rows = await tariffs.tariff_history(session, wt.id)
    lines = [f"<b>{h(wt.name)}</b> — история ставок (новые сверху)"]
    for t, dept_code, emp_code in rows[:50]:
        scope = " · ".join(filter(None, [dept_code and f"подр. {dept_code}", emp_code and f"сотр. {emp_code}"]))
        lines.append(f"#{t.id} с {t.valid_from:%d.%m.%Y}: {fmt_number(t.rate)} {t.currency} · {h(scope or 'общий')}")
    notifier.answer(message, "\n".join(lines))
