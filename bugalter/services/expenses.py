"""Расходы компании: реклама, сотрудничество, аренда и т. п. Удаление только пометкой."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Expense
from ..domain.errors import AccessDenied, NotFound, ValidationError
from ..domain.permissions import Perm
from .audit import audit

CATEGORIES = ("Реклама", "Сотрудничество", "Аренда", "Закупка", "Транспорт", "Связь", "Прочее")


def require(actor) -> None:
    if not (actor.is_owner or actor.can_anywhere(Perm.PAYROLL_PAYOUT)):
        raise AccessDenied("Расходы ведут владелец, бухгалтер и руководитель")


async def add(
    session: AsyncSession,
    actor,
    *,
    category: str,
    amount: Decimal,
    currency: str,
    spent_on: dt.date,
    comment: str | None,
) -> Expense:
    require(actor)
    category = " ".join(category.split())[:64]
    if not category:
        raise ValidationError("Укажите, на что расход")
    if amount <= 0:
        raise ValidationError("Сумма должна быть больше нуля")
    expense = Expense(
        category=category,
        amount=amount,
        currency=currency,
        spent_on=spent_on,
        comment=comment or None,
        created_by=actor.user.id,
    )
    session.add(expense)
    await session.flush()
    await audit(session, actor.user.id, "expense.add", "expense", expense.id, {"category": category, "amount": amount})
    return expense


async def delete(session: AsyncSession, actor, expense_id: int) -> Expense:
    require(actor)
    expense = await session.get(Expense, expense_id)
    if expense is None or expense.deleted_at is not None:
        raise NotFound("Расход не найден или уже удалён")
    expense.deleted_at = dt.datetime.now(dt.UTC)
    expense.deleted_by = actor.user.id
    await audit(session, actor.user.id, "expense.delete", "expense", expense.id, None)
    return expense


async def totals(
    session: AsyncSession, date_from: dt.date, date_to: dt.date, currency: str
) -> list[tuple[str, Decimal]]:
    rows = await session.execute(
        select(Expense.category, func.sum(Expense.amount))
        .where(
            Expense.deleted_at.is_(None),
            Expense.currency == currency,
            Expense.spent_on.between(date_from, date_to),
        )
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc())
    )
    return [(c, a) for c, a in rows.all()]


async def recent(session: AsyncSession, date_from: dt.date, date_to: dt.date, limit: int = 15) -> list[Expense]:
    rows = await session.execute(
        select(Expense)
        .where(Expense.deleted_at.is_(None), Expense.spent_on.between(date_from, date_to))
        .order_by(Expense.spent_on.desc(), Expense.id.desc())
        .limit(limit)
    )
    return list(rows.scalars())
