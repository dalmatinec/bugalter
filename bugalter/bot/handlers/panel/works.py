"""Работы и цены: виды работ, как платим, цена для всех, для филиала и для конкретного сотрудника."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from ....db.models import Department, Employee, WorkType
from ....domain.errors import NotFound, ValidationError
from ....domain.money import currency_label, fmt_number, parse_decimal
from ....domain.permissions import Perm
from ....services import org, reference, tariffs, work
from ...ui import h
from .base import ask, cancel_kb, form, kb, rate_text, show

router = Router(name="panel.works")

METHOD_TITLES = {
    "per_unit": "за единицу (количество × ставка, можно со ступенями)",
    "fixed": "фиксированная сумма за операцию",
    "percent": "процент от суммы заказа",
}
PAY_BUTTONS = [
    ("📦 За штуку / заказ / кг / час", "per_unit"),
    ("💵 Фиксированно за раз", "fixed"),
    ("％ Процент от суммы заказа", "percent"),
]
COMMON_UNITS = ("заказ", "шт", "г", "кг", "л", "ч", "адрес", "заявка")


async def works_view(session, settings):
    lines = [
        "💰 <b>Работы и цены</b>",
        "За что платим и сколько. Нажмите на работу, чтобы поменять цену.",
        "Сотрудник отчитывается в своей группе тегом и количеством: <code>#доставка 3</code>",
        "",
    ]
    rows = []
    today = settings.today()
    for wt in await tariffs.visible_work_types(session):
        rates = [r for r in await tariffs.current_rates(session, wt.id, today) if not r[4]]
        base = next((r for r in rates if r[1] is None and r[2] is None), rates[0] if rates else None)
        price = rate_text(base[0].rate, base[0].currency, wt.calc_method) if base else "цена не задана ⚠️"
        if base and wt.calc_method == "per_unit":
            price += f" за {h(wt.unit_code)}"
        elif base and wt.calc_method == "fixed":
            price += " за раз"
        state = f" #{h(tariffs.work_tag(wt.name))}" if wt.is_active else " (выключена)"
        lines.append(f"• <b>{h(wt.name)}</b>{state}: {price}")
        rows.append([(f"{wt.name}", f"p:w:v:{wt.id}")])
    if not rows:
        lines.append("Пока пусто. Добавьте первую работу, например Доставка 👇")
    rows.append([("➕ Добавить работу", "p:w:add")])
    rows.append([("⬅️ В меню", "p:home")])
    return "\n".join(lines), kb(rows)


async def work_view(session, settings, wt_id: int):
    wt = await session.get(WorkType, wt_id)
    if wt is None:
        raise NotFound("Вид работ не найден")
    today = settings.today()
    tag = tariffs.work_tag(wt.name)
    lines = [
        f"💰 <b>{h(wt.name)}</b>",
        f"Оплата: {METHOD_TITLES[wt.calc_method]}"
        + (f", считаем в {h(wt.unit_code)}" if wt.calc_method == "per_unit" else ""),
        f"Тег для отчёта в группе: <code>#{h(tag)} 3</code> (3 значит сколько сделано)",
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
    if not single_dept:
        rows.append([("🏢 Цена филиала", f"p:w:t:{wt.id}")])
    rows.append([("⏸ Выключить" if wt.is_active else "▶️ Включить", f"p:w:tg:{wt.id}")])
    rows.append([("⬅️ Работы и цены", "p:w")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "p:w")
async def works(callback: CallbackQuery, state: FSMContext, session, settings, notifier):
    await state.clear()
    show(callback, notifier, *await works_view(session, settings))


@router.callback_query(F.data.startswith("p:w:v:"))
async def work_type(callback: CallbackQuery, state: FSMContext, session, settings, notifier):
    await state.clear()
    show(callback, notifier, *await work_view(session, settings, int(callback.data.split(":")[3])))


@router.callback_query(F.data.startswith("p:w:tg:"))
async def work_toggle(callback: CallbackQuery, session, actor, settings, notifier):
    wt = await session.get(WorkType, int(callback.data.split(":")[3]))
    await tariffs.set_work_type_active(session, actor, wt, not wt.is_active)
    await session.flush()
    show(callback, notifier, *await work_view(session, settings, wt.id))


@router.callback_query(F.data == "p:w:add")
async def work_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    actor.require(Perm.WORKTYPE_MANAGE)
    await ask(state, "wt_name")
    show(
        callback,
        notifier,
        "✏️ Как называется работа? Например: <b>Доставка</b>, <b>Фасовка</b>, <b>Погрузка</b>",
        cancel_kb("p:w"),
    )


@form("wt_name")
async def form_wt_name(message, state, session, notifier, **_):
    name = " ".join(message.text.split())
    await state.update_data(wt_name=name)
    rows = [[(title, f"p:w:m:{code}")] for title, code in PAY_BUTTONS]
    rows.append([("✖️ Отмена", "p:w")])
    notifier.answer(message, f"{h(name)}: как платим?", kb(rows))


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
        show(
            callback,
            notifier,
            f"{h(data['wt_name'])}: за что платим? Например, за заказ, за штуку, за кг.\n\n"
            "💡 <b>Работа по весу</b> (фасовка, факт, готовые, доставка товара): выберите г, кг или л "
            "и потом укажите цену за 1 г (кг, л). Тогда не нужно заводить работу на каждый товар и вес: "
            "сотрудник пишет <code>Крам факт 0.5 13</code>, и бот сам считает 13 шт × 0,5 × цена.",
            kb(grid),
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
        show(callback, notifier, "Выберите единицу:", kb(grid))
        return
    await _create_work(callback, state, session, actor, settings, notifier, "per_unit", unit)


async def _create_work(callback, state, session, actor, settings, notifier, method: str, unit: str):
    data = await state.get_data()
    if "wt_name" not in data:
        raise ValidationError("Начните заново: Работы и цены → Добавить работу")
    code = await org.unique_code(session, WorkType.code, data["wt_name"])
    wt = await tariffs.create_work_type(session, actor, code, unit, method, data["wt_name"])
    await session.flush()
    await ask(state, "tariff_rate", wt_id=wt.id, dept_id=None, emp_id=None)
    show(
        callback,
        notifier,
        "✅ Работа добавлена.\n\n" + _rate_prompt(wt, settings, "для всех"),
        cancel_kb(f"p:w:v:{wt.id}"),
    )


@router.callback_query(F.data.startswith("p:w:t:"))
async def work_rate_scope(callback: CallbackQuery, session, notifier):
    wt = await session.get(WorkType, int(callback.data.split(":")[3]))
    rows = [[("🌐 Для всех", f"p:w:s:{wt.id}:0")]]
    for dept in await org.list_departments(session):
        rows.append([(f"🏢 {dept.name}", f"p:w:s:{wt.id}:{dept.id}")])
    rows.append([("👤 Для сотрудника", f"p:w:e:{wt.id}")])
    rows.append([("⬅️ Назад", f"p:w:v:{wt.id}")])
    show(callback, notifier, f"💰 Цена {h(wt.name)}: для кого?", kb(rows))


@router.callback_query(F.data.startswith("p:w:e:"))
async def work_rate_employee(callback: CallbackQuery, session, notifier):
    wt_id = int(callback.data.split(":")[3])
    rows = [
        [(f"👤 {e.full_name}", f"p:w:x:{wt_id}:{e.id}")] for e, _ in await org.list_employees(session) if e.is_active
    ]
    rows.append([("⬅️ Назад", f"p:w:v:{wt_id}")])
    show(callback, notifier, "Кому своя цена?", kb(rows))


def _rate_prompt(wt: WorkType, settings, scope: str) -> str:
    cur = currency_label(settings.currency)
    who = "" if scope == "для всех" else f" ({h(scope)})"
    if wt.calc_method == "percent":
        what = f"💰 Какой процент от суммы заказа платим{who}? Напишите число, например <b>3.5</b>"
    elif wt.calc_method == "fixed":
        what = f"💰 Сколько платим за один раз{who}? Напишите сумму в {cur}, например <b>700</b>"
    elif wt.unit_code in ("г", "кг", "л"):
        what = (
            f"💰 Сколько платим за 1 {h(wt.unit_code)}{who}? Напишите сумму в {cur}, например <b>400</b>\n\n"
            f"Сотрудник напишет товар, вес одной штуки и сколько штук: <code>Крам {h(wt.name)} 0.5 13</code>. "
            f"Бот посчитает 13 × 0,5 = 6,5 {h(wt.unit_code)} × цена.\n"
            f"<i>Если для какого-то веса цена особая, заведите ещё работу с весом в названии, например "
            f"<b>{h(wt.name)} 0.5</b>, она будет важнее.</i>"
        )
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
    await ask(state, "tariff_rate", wt_id=wt.id, dept_id=dept_id, emp_id=emp_id)
    show(callback, notifier, _rate_prompt(wt, settings, scope), cancel_kb(f"p:w:v:{wt.id}"))


@form("tariff_rate")
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
