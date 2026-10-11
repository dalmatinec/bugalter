"""Журнал взаиморасчётов и расчётные периоды.

Все деньги сотрудника — это сумма строк ledger_entries. Строки не меняются; ошибка исправляется
сторнирующей строкой. Строка всегда попадает в открытый период: если период даты операции закрыт,
она проводится в текущем открытом периоде с исходной датой (business_date) для расшифровки.
"""

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import LedgerEntry, Payout, PayoutRequest, PayrollPeriod
from ..domain.dates import semi_monthly_bounds
from ..domain.errors import ValidationError
from .settings import Settings


async def period_for_date(session: AsyncSession, day: dt.date, settings: Settings) -> PayrollPeriod:
    existing = (
        await session.execute(select(PayrollPeriod).where(PayrollPeriod.date_from <= day, PayrollPeriod.date_to >= day))
    ).scalar_one_or_none()
    if existing:
        return existing
    start, end = semi_monthly_bounds(day, settings.payroll_split_day)
    # Если день разделения меняли, не даём новому периоду наехать на соседние.
    prev_end = (
        await session.execute(select(func.max(PayrollPeriod.date_to)).where(PayrollPeriod.date_to < day))
    ).scalar()
    next_start = (
        await session.execute(select(func.min(PayrollPeriod.date_from)).where(PayrollPeriod.date_from > day))
    ).scalar()
    if prev_end and prev_end >= start:
        start = prev_end + dt.timedelta(days=1)
    if next_start and next_start <= end:
        end = next_start - dt.timedelta(days=1)
    stmt = insert(PayrollPeriod).values(date_from=start, date_to=end, status="open", version=0)
    await session.execute(stmt.on_conflict_do_nothing())
    return (
        await session.execute(select(PayrollPeriod).where(PayrollPeriod.date_from <= day, PayrollPeriod.date_to >= day))
    ).scalar_one()


async def posting_period(session: AsyncSession, business_date: dt.date, settings: Settings) -> PayrollPeriod:
    """Открытый период для проводки: свой, а если он закрыт — первый открытый после него."""
    period = await period_for_date(session, business_date, settings)
    if period.status == "open":
        return period
    today_period = await period_for_date(session, max(settings.today(), business_date), settings)
    if today_period.status == "open":
        return today_period
    candidate = (
        await session.execute(
            select(PayrollPeriod)
            .where(PayrollPeriod.status == "open", PayrollPeriod.date_from > period.date_to)
            .order_by(PayrollPeriod.date_from)
            .limit(1)
        )
    ).scalar_one_or_none()
    if candidate:
        return candidate
    # Все известные периоды закрыты (закрыли досрочно) — открываем следующий.
    day = today_period.date_to + dt.timedelta(days=1)
    while True:
        nxt = await period_for_date(session, day, settings)
        if nxt.status == "open":
            return nxt
        day = nxt.date_to + dt.timedelta(days=1)


async def post(
    session: AsyncSession,
    settings: Settings,
    *,
    employee_id: int,
    department_id: int | None,
    currency: str,
    amount: Decimal,
    entry_type: str,
    source_type: str,
    source_id: int,
    business_date: dt.date,
    posted_by: int | None,
    comment: str | None = None,
    reverses_id: int | None = None,
) -> tuple[LedgerEntry, bool]:
    """Проводка. Повтор для того же документа не создаёт вторую строку: возвращает существующую."""
    if amount == 0:
        raise ValidationError("Нулевая сумма не проводится")
    period = await posting_period(session, business_date, settings)
    stmt = (
        insert(LedgerEntry)
        .values(
            employee_id=employee_id,
            department_id=department_id,
            currency=currency,
            amount=amount,
            entry_type=entry_type,
            source_type=source_type,
            source_id=source_id,
            business_date=business_date,
            period_id=period.id,
            posted_by=posted_by,
            comment=comment,
            reverses_id=reverses_id,
        )
        .on_conflict_do_nothing(index_elements=["source_type", "source_id", "entry_type"])
        .returning(LedgerEntry.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is not None:
        return await session.get(LedgerEntry, new_id), True
    existing = (
        await session.execute(
            select(LedgerEntry).where(
                LedgerEntry.source_type == source_type,
                LedgerEntry.source_id == source_id,
                LedgerEntry.entry_type == entry_type,
            )
        )
    ).scalar_one()
    return existing, False


async def reverse(
    session: AsyncSession,
    settings: Settings,
    original: LedgerEntry,
    *,
    posted_by: int,
    comment: str,
) -> LedgerEntry:
    entry, _ = await post(
        session,
        settings,
        employee_id=original.employee_id,
        department_id=original.department_id,
        currency=original.currency,
        amount=-original.amount,
        entry_type="reversal",
        source_type=original.source_type,
        source_id=original.source_id,
        business_date=original.business_date,
        posted_by=posted_by,
        comment=comment,
        reverses_id=original.id,
    )
    return entry


async def find_entry(session: AsyncSession, source_type: str, source_id: int, entry_type: str) -> LedgerEntry | None:
    return (
        await session.execute(
            select(LedgerEntry).where(
                LedgerEntry.source_type == source_type,
                LedgerEntry.source_id == source_id,
                LedgerEntry.entry_type == entry_type,
            )
        )
    ).scalar_one_or_none()


async def balances(session: AsyncSession, employee_id: int) -> dict[str, Decimal]:
    rows = (
        await session.execute(
            select(LedgerEntry.currency, func.sum(LedgerEntry.amount))
            .where(LedgerEntry.employee_id == employee_id)
            .group_by(LedgerEntry.currency)
        )
    ).all()
    return {currency: total for currency, total in rows}


async def reserved(session: AsyncSession, employee_id: int) -> dict[str, Decimal]:
    """Деньги, уже обещанные к выплате: зарегистрированные, но не подтверждённые выплаты и открытые запросы."""
    result: dict[str, Decimal] = defaultdict(Decimal)
    payouts = (
        await session.execute(
            select(Payout.currency, func.sum(Payout.amount))
            .where(Payout.employee_id == employee_id, Payout.status == "registered")
            .group_by(Payout.currency)
        )
    ).all()
    requests = (
        await session.execute(
            select(PayoutRequest.currency, func.sum(PayoutRequest.amount))
            .where(PayoutRequest.employee_id == employee_id, PayoutRequest.status == "requested")
            .group_by(PayoutRequest.currency)
        )
    ).all()
    for currency, total in [*payouts, *requests]:
        result[currency] += total
    return dict(result)


async def available(session: AsyncSession, employee_id: int, currency: str, exclude_request_id: int | None = None):
    balance = (await balances(session, employee_id)).get(currency, Decimal(0))
    held = (await reserved(session, employee_id)).get(currency, Decimal(0))
    if exclude_request_id is not None:
        req = await session.get(PayoutRequest, exclude_request_id)
        if req and req.status == "requested" and req.currency == currency:
            held -= req.amount
    return balance - held
