"""Отчёты: /report <вид> [объект] [период] [xlsx|csv]. Только в личке."""

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ...domain.dates import looks_like_range, parse_range
from ...domain.errors import AccessDenied, ValidationError
from ...domain.permissions import Perm
from ...services import ledger, org, reports
from ..args import extract, resolve_department, tokens
from ..ui import h
from .payroll import _can_view_employee, _statement_for

router = Router(name="reports")

USAGE = """<b>Отчёты</b>: /report &lt;вид&gt; … [период] [xlsx|csv]
• emp &lt;сотрудник&gt; [прошлый|п12|месяц|01.10-15.10]: расчёт с расшифровкой
• dept [@подр] [период]: подразделение по сотрудникам
• ops [@подр] [период]: все операции
• stock [@подр]: остатки, moves [@подр] [период]: движение товара
• payouts [@подр] [период]: выплаты, adj [@подр] [период]: премии/штрафы/удержания
• debts [@подр]: остатки взаиморасчётов (кому должны / кто должен)
Период: сегодня, вчера, неделя, месяц, прошлый_месяц, ДД.ММ, ДД.ММ-ДД.ММ"""


@router.message(Command("report"))
async def report(message: Message, command: CommandObject, session, actor, settings, chat_dept, notifier, is_private):
    if not is_private:
        raise ValidationError("Отчёты с суммами формируются только в личном чате с ботом")
    ex = extract(tokens(command.args))
    if not ex.rest:
        notifier.answer(message, USAGE)
        return
    kind, rest = ex.rest[0].lower(), ex.rest[1:]
    fmt = None
    if rest and rest[-1].lower() in ("xlsx", "csv"):
        fmt = rest.pop().lower()
    today = settings.today()
    if fmt and not actor.can_anywhere(Perm.REPORT_EXPORT) and not actor.is_owner:
        raise AccessDenied("Нет права на выгрузку отчётов")

    if kind == "emp":
        if not rest:
            raise ValidationError("Формат: /report emp <сотрудник> [период]")
        emp = await org.get_employee(session, rest[0])
        if not await _can_view_employee(session, actor, emp):
            raise AccessDenied("Недостаточно прав")
        st = await _statement_for(session, settings, emp, rest[1] if len(rest) > 1 else None)
        table = await reports.employee_report(session, emp, st)
    elif kind == "debts":
        depts = actor.departments_with(Perm.REPORT_VIEW)
        if ex.dept:
            dept = await org.get_department(session, ex.dept)
            actor.require(Perm.REPORT_VIEW, dept.id)
            depts = {dept.id}
        elif depts == set():
            raise AccessDenied("Нет права на отчёты")
        table = await reports.balances_report(session, depts)
    elif kind in ("payouts", "adj"):
        depts = actor.departments_with(Perm.REPORT_VIEW)
        if ex.dept:
            dept = await org.get_department(session, ex.dept)
            actor.require(Perm.REPORT_VIEW, dept.id)
            depts = {dept.id}
        elif depts == set():
            raise AccessDenied("Нет права на отчёты")
        date_from, date_to = parse_range(rest[0] if rest else "месяц", today)
        fn = reports.payouts_report if kind == "payouts" else reports.adjustments_report
        table = await fn(session, date_from, date_to, depts)
    elif kind in ("dept", "ops", "stock", "moves"):
        dept = await resolve_department(session, actor, chat_dept, ex.dept, Perm.REPORT_VIEW)
        actor.require(Perm.REPORT_VIEW, dept.id)
        period_arg = rest[0] if rest and looks_like_range(rest[0]) else None
        if kind == "dept":
            if period_arg is None:
                p = await ledger.period_for_date(session, today, settings)
                date_from, date_to = p.date_from, p.date_to
            else:
                date_from, date_to = parse_range(period_arg, today)
            table = await reports.department_summary(session, dept, date_from, date_to)
        elif kind == "ops":
            date_from, date_to = parse_range(period_arg or "сегодня", today)
            table = await reports.operations_report(session, dept, date_from, date_to, show_amounts=True)
        elif kind == "stock":
            table = await reports.stock_report(session, dept)
        else:
            date_from, date_to = parse_range(period_arg or "неделя", today)
            table = await reports.moves_report(session, dept, date_from, date_to, settings.timezone)
    else:
        notifier.answer(message, USAGE)
        return

    if fmt == "csv":
        notifier.document(message.chat.id, table.to_csv(), f"{kind}_{today:%Y%m%d}.csv", table.title)
    elif fmt == "xlsx":
        notifier.document(message.chat.id, table.to_xlsx(), f"{kind}_{today:%Y%m%d}.xlsx", table.title)
    else:
        text = h(reports.render_text(table))
        if len(text) > 4000:
            text = text[:3900] + "\n...\nОтчёт длинный, добавьте xlsx в конце команды."
        notifier.answer(message, text)
