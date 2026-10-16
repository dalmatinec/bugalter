"""Отчёты кнопками: Срок 1 и Срок 2 месяца, месяц, квартал или свои даты. Текстом и в Excel."""

import calendar
import datetime as dt
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from ....domain.dates import parse_range
from ....domain.errors import AccessDenied
from ....domain.money import fmt_money
from ....domain.permissions import Perm
from ....services import fixed_salary, reports
from ...ui import h
from .base import ask, cancel_kb, form, kb, show

router = Router(name="panel.reports")

MONTHS = [
    "",
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
]


def _depts(actor) -> set[int] | None:
    if actor.is_owner:
        return None
    # Деньги всех сотрудников видят те, кто может выгружать отчёты: бухгалтер и руководитель.
    if not actor.can_anywhere(Perm.REPORT_EXPORT):
        raise AccessDenied("Отчёты доступны владельцу, бухгалтеру и руководителю")
    return actor.departments_with(Perm.REPORT_EXPORT)


def _month_end(day: dt.date) -> dt.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _prev_month(day: dt.date) -> dt.date:
    return day.replace(day=1) - dt.timedelta(days=1)


def periods(today: dt.date, split: int) -> list[tuple[str, dt.date, dt.date]]:
    """Готовые периоды для кнопок."""
    split = min(split, _month_end(today).day - 1)
    prev = _prev_month(today)
    prev_split = min(split, _month_end(prev).day - 1)
    q = (today.month - 1) // 3
    q_start = dt.date(today.year, q * 3 + 1, 1)
    pq_end = q_start - dt.timedelta(days=1)
    pq_start = dt.date(pq_end.year, (pq_end.month - 1) // 3 * 3 + 1, 1)
    end = _month_end(today)
    return [
        (f"Срок 1: 1-{split} {MONTHS[today.month]}", today.replace(day=1), today.replace(day=split)),
        (f"Срок 2: {split + 1}-{end.day} {MONTHS[today.month]}", today.replace(day=split + 1), end),
        (f"Срок 2 прошлого: {prev_split + 1}-{prev.day} {MONTHS[prev.month]}", prev.replace(day=prev_split + 1), prev),
        (f"Месяц: {MONTHS[today.month]}", today.replace(day=1), _month_end(today)),
        (f"Прошлый месяц: {MONTHS[prev.month]}", prev.replace(day=1), prev),
        (f"Квартал: {q + 1}-й", q_start, _month_end(q_start.replace(month=q * 3 + 3))),
        (f"Прошлый квартал: {(pq_start.month - 1) // 3 + 1}-й", pq_start, pq_end),
    ]


def _code(day: dt.date) -> str:
    return day.strftime("%Y%m%d")


def _day(code: str) -> dt.date:
    return dt.datetime.strptime(code, "%Y%m%d").date()


@router.callback_query(F.data == "p:r")
async def reports_home(callback: CallbackQuery, state: FSMContext, actor, settings, notifier):
    _depts(actor)
    await state.clear()
    choices = periods(settings.today(), settings.payroll_split_day)
    rows = [[(title, f"p:r:v:{_code(a)}:{_code(b)}")] for title, a, b in choices]
    rows.append([("✏️ Свои даты", "p:r:in")])
    rows.append([("⬅️ В меню", "p:home")])
    text = (
        "📊 <b>Отчёты</b>\n\n"
        "Выберите период. Бот покажет по каждому сотруднику: сколько заработал, премии и штрафы, "
        "сколько выдано авансом и зарплатой и сколько ему ещё должны. Ниже будет кнопка скачать в Excel.\n\n"
        f"Срок 1 это с 1 по {settings.payroll_split_day} число, Срок 2 с {settings.payroll_split_day + 1} "
        "до конца месяца. Поменять границу: Ещё, Настройки, Периоды."
    )
    show(callback, notifier, text, kb(rows))


@router.callback_query(F.data == "p:r:in")
async def reports_custom(callback: CallbackQuery, state: FSMContext, actor, notifier):
    _depts(actor)
    await ask(state, "report_range")
    show(
        callback,
        notifier,
        "✏️ Напишите даты через тире, например <b>01.09-30.11</b>\nИли одно слово: неделя, месяц, прошлый_месяц",
        cancel_kb("p:r"),
    )


@form("report_range")
async def form_report_range(message, state, session, actor, settings, notifier, **_):
    date_from, date_to = parse_range(message.text, settings.today())
    await state.clear()
    notifier.answer(message, *await summary_view(session, actor, settings, date_from, date_to))


async def summary_view(session, actor, settings, date_from: dt.date, date_to: dt.date):
    await fixed_salary.accrue_all(session, settings)
    cur = settings.currency
    rows = await reports.period_summary(session, date_from, date_to, _depts(actor), cur)
    lines = [f"📊 <b>Отчёт {date_from:%d.%m.%Y} - {date_to:%d.%m.%Y}</b>", ""]

    def m(value):
        return fmt_money(value, cur)

    for r in rows:
        lines.append(f"👤 <b>{h(r.employee.full_name)}</b>")
        lines.append(f"   заработал {m(r.earned)}" + (f", премии {m(r.bonus)}" if r.bonus else ""))
        if r.minus:
            lines.append(f"   штрафы и удержания {m(r.minus)}")
        if r.advance_paid or r.salary_paid:
            lines.append(f"   выдано: аванс {m(r.advance_paid)}, зарплата {m(r.salary_paid)}")
        tail = f"   сейчас ему должны {m(r.balance)}" if r.balance >= 0 else f"   сейчас он должен {m(-r.balance)}"
        if r.advance_left:
            tail += f", аванс не вычтен {m(r.advance_left)}"
        lines.append(tail)
    if rows:
        total = sum((r.earned + r.bonus for r in rows), Decimal(0))
        paid = sum((r.advance_paid + r.salary_paid for r in rows), Decimal(0))
        lines += ["", f"Итого заработано {m(total)}, выдано {m(paid)}"]
    else:
        lines.append("За эти даты ничего нет.")
    markup = kb(
        [
            [("📥 Скачать Excel", f"p:r:x:{_code(date_from)}:{_code(date_to)}")],
            [("⬅️ Другой период", "p:r")],
        ]
    )
    return "\n".join(lines), markup


@router.callback_query(F.data.startswith("p:r:v:"))
async def report_show(callback: CallbackQuery, session, actor, settings, notifier):
    _, _, _, a, b = callback.data.split(":")
    show(callback, notifier, *await summary_view(session, actor, settings, _day(a), _day(b)))


@router.callback_query(F.data.startswith("p:r:x:"))
async def report_excel(callback: CallbackQuery, session, actor, settings, notifier):
    _, _, _, a, b = callback.data.split(":")
    date_from, date_to = _day(a), _day(b)
    await fixed_salary.accrue_all(session, settings)
    rows = await reports.period_summary(session, date_from, date_to, _depts(actor), settings.currency)
    table = reports.summary_table(rows, date_from, date_to, settings.currency)
    notifier.document(
        callback.message.chat.id, table.to_xlsx(), f"otchet_{date_from:%d%m}-{date_to:%d%m%Y}.xlsx", table.title
    )
    notifier.toast(callback, "Отправляю файл")
