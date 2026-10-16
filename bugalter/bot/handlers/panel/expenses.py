"""Расходы кнопками: реклама, сотрудничество, аренда и т. п. Мелкие и средние траты компании."""

import calendar
import datetime as dt

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from ....domain.dates import looks_like_date, parse_date
from ....domain.money import fmt_money, parse_decimal
from ....services import expenses
from ...ui import h
from .base import ask, cancel_kb, form, kb, show

router = Router(name="panel.expenses")


def _month_bounds(first: dt.date) -> tuple[dt.date, dt.date]:
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


async def expenses_view(session, settings, first: dt.date):
    date_from, date_to = _month_bounds(first)
    cur = settings.currency
    lines = [f"🧾 <b>Расходы</b> · {first:%m.%Y}", ""]
    totals = await expenses.totals(session, date_from, date_to, cur)
    for category, amount in totals:
        lines.append(f"• {h(category)}: <b>{fmt_money(amount, cur)}</b>")
    if totals:
        lines.append(f"Итого: <b>{fmt_money(sum(a for _, a in totals), cur)}</b>")
    else:
        lines.append("В этом месяце расходов нет.")
    rows = [[("➕ Добавить расход", "p:x:add")]]
    recent = await expenses.recent(session, date_from, date_to)
    if recent:
        lines += ["", "<b>Последние</b> (🗑 удалить, если записали по ошибке):"]
        for e in recent:
            note = f", {h(e.comment)}" if e.comment else ""
            lines.append(f"{e.spent_on:%d.%m} {h(e.category)}: {fmt_money(e.amount, e.currency)}{note}")
            rows.append([(f"🗑 {e.spent_on:%d.%m} {e.category} {fmt_money(e.amount, e.currency)}", f"p:x:del:{e.id}")])
    prev = (first - dt.timedelta(days=1)).replace(day=1)
    nav = [(f"⬅️ {prev:%m.%Y}", f"p:x:m:{prev:%Y%m}")]
    nxt = date_to + dt.timedelta(days=1)
    if nxt <= settings.today():
        nav.append((f"{nxt:%m.%Y} ➡️", f"p:x:m:{nxt:%Y%m}"))
    rows.append(nav)
    rows.append([("⬅️ В меню", "p:home")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "p:x")
async def expenses_home(callback: CallbackQuery, state: FSMContext, session, actor, settings, notifier):
    expenses.require(actor)
    await state.clear()
    show(callback, notifier, *await expenses_view(session, settings, settings.today().replace(day=1)))


@router.callback_query(F.data.startswith("p:x:m:"))
async def expenses_month(callback: CallbackQuery, session, actor, settings, notifier):
    expenses.require(actor)
    code = callback.data.split(":")[3]
    show(callback, notifier, *await expenses_view(session, settings, dt.date(int(code[:4]), int(code[4:]), 1)))


@router.callback_query(F.data == "p:x:add")
async def expense_add(callback: CallbackQuery, actor, notifier):
    expenses.require(actor)
    buttons = [(c, f"p:x:c:{i}") for i, c in enumerate(expenses.CATEGORIES)]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([("✏️ Своя статья", "p:x:c:own"), ("✖️ Отмена", "p:x")])
    show(callback, notifier, "🧾 На что расход?", kb(rows))


@router.callback_query(F.data.startswith("p:x:c:"))
async def expense_category(callback: CallbackQuery, state: FSMContext, actor, settings, notifier):
    expenses.require(actor)
    choice = callback.data.split(":")[3]
    if choice == "own":
        await ask(state, "expense_category")
        show(callback, notifier, "✏️ Напишите статью расхода, например <b>Упаковка</b>", cancel_kb("p:x"))
        return
    await _ask_amount(callback, state, settings, notifier, expenses.CATEGORIES[int(choice)])


async def _ask_amount(event, state, settings, notifier, category: str) -> None:
    await ask(state, "expense_amount", category=category)
    show(
        event,
        notifier,
        f"🧾 {h(category)}: сколько потратили? Можно с комментарием и датой:\n"
        f"<b>50000 таргет инстаграм</b> или <b>12000 вчера бензин</b>\nСумма в {settings.currency}.",
        cancel_kb("p:x"),
    )


@form("expense_category")
async def form_expense_category(message, state, settings, notifier, **_):
    await _ask_amount(message, state, settings, notifier, message.text.strip())


@form("expense_amount")
async def form_expense_amount(message, state, session, actor, settings, notifier, data, **_):
    words = message.text.replace(",", ".").split()
    amount = parse_decimal(words[0] if words else "", what="сумму")
    rest = words[1:]
    spent_on = settings.today()
    date_word = next((w for w in rest if looks_like_date(w)), None)
    if date_word:
        spent_on = parse_date(date_word, settings.today())
        rest.remove(date_word)
    expense = await expenses.add(
        session,
        actor,
        category=data["category"],
        amount=amount,
        currency=settings.currency,
        spent_on=spent_on,
        comment=" ".join(rest) or None,
    )
    await state.clear()
    note = f"\n💬 {h(expense.comment)}" if expense.comment else ""
    notifier.answer(
        message,
        f"✅ Расход записан: {h(expense.category)} {fmt_money(expense.amount, expense.currency)}, "
        f"{expense.spent_on:%d.%m.%Y}{note}",
        kb([[("🗑 Ошибка, удалить", f"p:x:del:{expense.id}"), ("🧾 Расходы", "p:x")]]),
    )


@router.callback_query(F.data.startswith("p:x:del:"))
async def expense_delete(callback: CallbackQuery, session, actor, settings, notifier):
    expense = await expenses.delete(session, actor, int(callback.data.split(":")[3]))
    notifier.toast(callback, "Расход удалён")
    show(callback, notifier, *await expenses_view(session, settings, expense.spent_on.replace(day=1)))
