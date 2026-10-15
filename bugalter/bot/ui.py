"""Тексты карточек и клавиатуры."""

from decimal import Decimal
from html import escape

from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    Adjustment,
    Currency,
    Department,
    Employee,
    Location,
    Payout,
    PayoutRequest,
    StockDocument,
    User,
    WorkEntry,
    WorkType,
)
from ..domain.money import fmt_money, fmt_number, fmt_qty, fmt_signed_money
from ..services import payroll, stock
from ..services.work import STATUS_TITLES

h = escape

STATUS_ICONS = {"draft": "📝", "pending": "⏳", "confirmed": "✅", "rejected": "❌", "cancelled": "🚫"}

BTN_MY_WORK = "📊 Моя работа"
BTN_MY_SALARY = "💰 Мой расчёт"
BTN_MY_STOCK = "📦 Мои товары"
BTN_REQUEST = "💸 Попросить аванс"
BTN_MY_PAYOUTS = "🧾 Мои выплаты"
BTN_HELP = "❓ Помощь"
BTN_PANEL = "⚙️ Управление"


def main_menu(admin: bool = False, employee: bool = True, stock: bool = False) -> ReplyKeyboardMarkup:
    """Меню внизу. Мои товары только у тех, кому выдавали товар; у владельца без карточки только Управление."""
    buttons = []
    if employee:
        buttons = [BTN_MY_WORK, BTN_MY_SALARY, BTN_REQUEST, BTN_MY_PAYOUTS]
        if stock:
            buttons.insert(2, BTN_MY_STOCK)
    if admin:
        buttons.insert(0, BTN_PANEL)
    buttons.append(BTN_HELP)
    rows = [[KeyboardButton(text=t) for t in buttons[i : i + 2]] for i in range(0, len(buttons), 2)]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def has_stock(session: AsyncSession, employee_id: int) -> bool:
    found = await session.execute(select(Location.id).where(Location.employee_id == employee_id).limit(1))
    return found.first() is not None


async def main_menu_for(session: AsyncSession, actor, admin: bool) -> ReplyKeyboardMarkup:
    emp = actor.employee
    stock_menu = emp is not None and await has_stock(session, emp.id)
    return main_menu(admin, employee=emp is not None, stock=stock_menu)


async def minor_units(session: AsyncSession, code: str) -> int:
    cur = await session.get(Currency, code)
    return cur.minor_units if cur else 2


async def money(session: AsyncSession, amount, code: str, signed: bool = False) -> str:
    places = await minor_units(session, code)
    return fmt_signed_money(amount, code, places) if signed else fmt_money(amount, code, places)


async def user_name(session: AsyncSession, user_id: int | None) -> str:
    if not user_id:
        return "-"
    user = await session.get(User, user_id)
    if user is None:
        return "-"
    return user.full_name or (f"@{user.username}" if user.username else str(user.telegram_id))


async def employee_label(session: AsyncSession, employee_id: int | None) -> str:
    """Сотрудника везде видно по должности (она и есть его подпись в боте)."""
    if not employee_id:
        return "-"
    emp = await session.get(Employee, employee_id)
    return emp.full_name if emp else "-"


async def actor_label(session: AsyncSession, user_id: int | None) -> str:
    """Кто нажал кнопку: у сотрудника его должность, у владельца без карточки - имя из Telegram."""
    if not user_id:
        return "-"
    emp = (await session.execute(select(Employee).where(Employee.user_id == user_id))).scalar_one_or_none()
    if emp is not None:
        return emp.full_name
    return await user_name(session, user_id)


def telegram_nick(user: User) -> str:
    return f"@{user.username}" if user.username else (user.full_name or "без ника")


# ------------------------------------------------------------------ операции


async def entry_card(session: AsyncSession, entry: WorkEntry, show_amount: bool) -> str:
    wt = await session.get(WorkType, entry.work_type_id)
    dept = await session.get(Department, entry.department_id)
    lines = [
        f"{STATUS_ICONS[entry.status]} <b>{entry.number}</b> · {STATUS_TITLES[entry.status]}",
        f"👤 {h(await employee_label(session, entry.employee_id))} · {h(dept.code)}",
        f"🛠 {h(wt.name)} × {fmt_qty(entry.quantity, entry.unit_code)}",
        f"📅 {entry.performed_on:%d.%m.%Y}" + (f" · 🧾 #{h(entry.order_ref)}" if entry.order_ref else ""),
    ]
    if entry.comment:
        lines.append(f"💬 {h(entry.comment)}")
    if entry.coefficient_codes:
        lines.append("⚡ " + ", ".join(h(c) for c in entry.coefficient_codes) + f" (×{fmt_number(entry.coefficient)})")
    if show_amount:
        lines.append(
            f"💰 {calc_text(wt.calc_method, entry)} = <b>{await money(session, entry.amount, entry.currency)}</b>"
        )
    lines.append(f"✍️ ввёл: {h(await actor_label(session, entry.created_by))}")
    if entry.decided_by:
        verb = "подтвердил" if entry.status in ("confirmed", "cancelled") else "решение"
        lines.append(f"👮 {verb}: {h(await actor_label(session, entry.decided_by))}")
    if entry.decision_comment:
        lines.append(f"📝 {h(entry.decision_comment)}")
    if entry.status == "cancelled":
        lines.append(f"🚫 отменил: {h(await actor_label(session, entry.cancelled_by))}, {h(entry.cancel_reason or '')}")
    return "\n".join(lines)


def calc_text(method: str, entry: WorkEntry) -> str:
    """Как получена сумма: "45 × 400 + 5 × 450", "3,5 % от 12 345,67", "фикс. 700", плюс коэффициент."""
    details = entry.calc_details or {}
    pieces = details.get("pieces") or [{"qty": str(entry.quantity), "rate": str(entry.rate)}]
    if method == "fixed":
        text = f"фикс. {fmt_number(entry.rate)}"
    elif method == "percent":
        text = f"{fmt_number(entry.rate)} % от {fmt_number(entry.quantity)}"
    else:
        text = " + ".join(f"{fmt_number(Decimal(p['qty']))} × {fmt_number(Decimal(p['rate']))}" for p in pieces)
        if details.get("tiered"):
            text += f" (ступени, до этого в периоде: {fmt_number(Decimal(details.get('prior_volume', '0')))})"
    if entry.coefficient != 1:
        text = f"({text}) × {fmt_number(entry.coefficient)}"
    return text


def entry_kb(entry: WorkEntry) -> InlineKeyboardMarkup | None:
    kb = InlineKeyboardBuilder()
    if entry.status == "draft":
        kb.button(text="📤 Отправить", callback_data=f"w:s:{entry.id}")
        kb.button(text="📅 Сегодня", callback_data=f"w:d:{entry.id}:0")
        kb.button(text="📅 Вчера", callback_data=f"w:d:{entry.id}:1")
        kb.button(text="✖️ Отменить", callback_data=f"w:x:{entry.id}")
        kb.adjust(1, 2, 1)
    elif entry.status == "pending":
        kb.button(text="✅ Подтвердить", callback_data=f"w:c:{entry.id}")
        kb.button(text="❌ Отклонить", callback_data=f"w:r:{entry.id}")
        kb.adjust(2)
    else:
        return None
    return kb.as_markup()


# ------------------------------------------------------------------ склад


async def stock_doc_card(session: AsyncSession, doc: StockDocument) -> str:
    dept = await session.get(Department, doc.department_id)
    status = {
        "posted": "проведён",
        "in_transit": "🚚 в пути, ждёт подтверждения получения",
        "completed": "✅ получено",
        "returned": "↩️ не принято, возвращено",
    }[doc.status]
    lines = [f"📦 <b>{doc.number}</b> · {stock.DOC_TITLES[doc.doc_type]} · {h(dept.code)}", status]
    for line, product in await stock.document_lines(session, doc.id):
        qty = fmt_qty(line.qty_entered, line.unit_entered)
        if line.unit_entered != product.base_unit:
            qty += f" (= {fmt_qty(line.qty_base, product.base_unit)})"
        lines.append(f"• {h(product.name)}: {qty}")
    for label, emp_id in (("от", doc.employee_id), ("кому", doc.target_employee_id)):
        if emp_id:
            lines.append(f"👤 {label}: {h(await employee_label(session, emp_id))}")
    if doc.target_department_id:
        target = await session.get(Department, doc.target_department_id)
        lines.append(f"🏢 в подразделение: {h(target.code)}")
    if doc.order_ref:
        lines.append(f"🧾 #{h(doc.order_ref)}")
    if doc.comment:
        lines.append(f"💬 {h(doc.comment)}")
    lines.append(f"✍️ {h(await actor_label(session, doc.created_by))}")
    if doc.accepted_by:
        lines.append(f"📥 {h(await actor_label(session, doc.accepted_by))}")
    return "\n".join(lines)


def stock_doc_kb(doc: StockDocument) -> InlineKeyboardMarkup | None:
    if doc.status != "in_transit":
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Получено", callback_data=f"sd:a:{doc.id}")
    kb.button(text="↩️ Не получено", callback_data=f"sd:d:{doc.id}")
    return kb.as_markup()


# ------------------------------------------------------------------ деньги


async def adjustment_card(session: AsyncSession, adj: Adjustment) -> str:
    emp = await session.get(Employee, adj.employee_id)
    sign = adj.amount if adj.kind == "bonus" else -adj.amount
    lines = [
        f"{'🎁' if adj.kind == 'bonus' else '⛔'} <b>{adj.number}</b> · {payroll.ADJ_TITLES[adj.kind]}",
        f"👤 {h(emp.full_name)}",
        f"💰 <b>{await money(session, sign, adj.currency, signed=True)}</b>",
        f"📄 Основание: {h(adj.reason)}",
        f"📅 {adj.business_date:%d.%m.%Y} · статус: {payroll.ADJ_STATUS_TITLES[adj.status]}",
        f"✍️ выписал: {h(await user_name(session, adj.created_by))}",
    ]
    if adj.decided_by:
        lines.append(f"🖋 подписал/решил: {h(await user_name(session, adj.decided_by))}")
    if adj.kind != "bonus" and adj.status == "approved" and adj.outstanding > 0:
        lines.append(
            f"⏳ Удержано {await money(session, adj.withheld, adj.currency)} из "
            f"{await money(session, adj.amount, adj.currency)}; остаток удерживается из следующих начислений "
            "(лимит удержаний)"
        )
    if adj.decision_comment:
        lines.append(f"📝 {h(adj.decision_comment)}")
    return "\n".join(lines)


def adjustment_kb(adj: Adjustment) -> InlineKeyboardMarkup | None:
    if adj.status != "pending":
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="🖋 Подписать", callback_data=f"adj:s:{adj.id}")
    kb.button(text="❌ Отклонить", callback_data=f"adj:r:{adj.id}")
    return kb.as_markup()


def consent_kb(adj: Adjustment) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Согласен", callback_data=f"adj:y:{adj.id}")
    kb.button(text="✋ Не согласен", callback_data=f"adj:n:{adj.id}")
    return kb.as_markup()


async def payout_card(session: AsyncSession, payout: Payout) -> str:
    emp = await session.get(Employee, payout.employee_id)
    lines = [
        f"💸 <b>{payout.number}</b> · {payroll.PAYOUT_KIND_TITLES[payout.kind]}",
        f"👤 {h(emp.full_name)}",
        f"💰 <b>{await money(session, payout.amount, payout.currency)}</b>",
        f"📅 {payout.business_date:%d.%m.%Y} · {payroll.PAYOUT_STATUS_TITLES[payout.status]}",
    ]
    if payout.method or payout.comment:
        lines.append("💬 " + h(" ".join(filter(None, [payout.method, payout.comment]))))
    lines.append(f"✍️ зарегистрировал: {h(await user_name(session, payout.registered_by))}")
    if payout.confirmed_by:
        lines.append(f"✅ подтвердил: {h(await user_name(session, payout.confirmed_by))}")
    if payout.status == "cancelled":
        lines.append(f"🚫 отменена: {h(payout.cancel_reason or '')}")
    return "\n".join(lines)


def payout_kb(payout: Payout) -> InlineKeyboardMarkup | None:
    if payout.status != "registered":
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить выплату", callback_data=f"po:c:{payout.id}")
    return kb.as_markup()


async def request_card(session: AsyncSession, req: PayoutRequest) -> str:
    emp = await session.get(Employee, req.employee_id)
    status = {
        "requested": "⏳ ожидает решения",
        "approved": "✅ одобрен",
        "rejected": "❌ отклонён",
        "cancelled": "🚫 отменён",
    }[req.status]
    text = (
        f"🙋 <b>{req.number}</b> · просит аванс\n👤 {h(emp.full_name)}\n"
        f"💰 <b>{await money(session, req.amount, req.currency)}</b>\n{status}"
    )
    if req.comment:
        text += f"\n💬 {h(req.comment)}"
    return text


def request_kb(req: PayoutRequest) -> InlineKeyboardMarkup | None:
    if req.status != "requested":
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"rq:a:{req.id}")
    kb.button(text="❌ Отклонить", callback_data=f"rq:r:{req.id}")
    return kb.as_markup()


def statement_text(st: payroll.Statement, with_available: bool = False) -> str:
    lines = [f"💰 <b>Расчёт: {h(st.employee.full_name)}</b>", st.title]
    if not st.currencies:
        lines.append("\nНачислений и выплат нет.")
    for code, cs in st.currencies.items():

        def m(v, signed=False, code=code, places=cs.minor_units):
            return (fmt_signed_money if signed else fmt_money)(v, code, places)

        lines.append(f"\n<b>{code}</b>")
        lines.append(f"Входящий остаток: {m(cs.opening)}")
        if cs.work_lines:
            lines.append("<u>Начислено за работу</u>")
            for wl in cs.work_lines:
                if wl.method == "fixed":
                    calc = f"{wl.count} оп. × фикс. {fmt_number(wl.rate)}"
                elif wl.method == "percent":
                    calc = f"{wl.count} оп., {fmt_number(wl.rate)} % от {fmt_number(wl.quantity)}"
                else:
                    calc = f"{wl.count} оп., {fmt_qty(wl.quantity, wl.unit)} × {fmt_number(wl.rate)}"
                    if wl.tiered:
                        calc += " (ступень)"
                if wl.coefficient != 1:
                    calc += f" × коэф. {fmt_number(wl.coefficient)}"
                lines.append(f"• {h(wl.title)}: {calc} = {m(wl.amount)}")
        if cs.other_lines:
            lines.append("<u>Премии, удержания, выплаты, корректировки</u>")
            for ol in cs.other_lines:
                lines.append(
                    f"• {ol.business_date:%d.%m} {payroll.LEDGER_TITLES[ol.entry_type]} "
                    f"{m(ol.amount, signed=True)}, {h(ol.comment)}"
                )
        lines.append("")
        lines.append(f"Начислено: {m(cs.accrued)}")
        lines.append(f"Удержано: {m(cs.deducted)}")
        if cs.corrections:
            lines.append(f"Корректировки: {m(cs.corrections, signed=True)}")
        lines.append(f"Выплачено: {m(cs.paid)}")
        lines.append(f"<b>Остаток: {m(cs.closing)}</b>" + (" (долг)" if cs.closing < 0 else ""))
        if cs.pending_count:
            lines.append(f"⏳ Ждёт подтверждения: {cs.pending_count} оп. на {m(cs.pending_amount)} (не входит)")
        if with_available and cs.reserved:
            lines.append(f"🔒 В обработке выплат: {m(cs.reserved)}; доступно: {m(cs.available)}")
        if with_available and cs.outstanding_withholding:
            lines.append(f"⏳ Ещё будет удержано (лимит удержаний): {m(cs.outstanding_withholding)}")
        if with_available and cs.advance:
            lines.append(f"💵 Аванс, ещё не вычтен из зарплаты: {m(cs.advance)}")
    return "\n".join(lines)
