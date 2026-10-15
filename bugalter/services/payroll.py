"""Премии, штрафы, удержания, выплаты, расчётные периоды и подробный расчёт сотрудника."""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..db.models import (
    Adjustment,
    AdjustmentInstallment,
    Currency,
    Employee,
    EmployeeCreditLimit,
    LedgerEntry,
    Payout,
    PayoutRequest,
    PayrollPeriod,
    PeriodReopening,
    PeriodStatement,
    WorkEntry,
    WorkType,
)
from ..domain.errors import AccessDenied, Conflict, DomainError, InsufficientFunds, NotFound, ValidationError
from ..domain.money import currency_step, fmt_money, round_to_step, scale_of
from ..domain.permissions import Perm
from . import ledger, org
from .audit import audit
from .settings import Settings, get_settings

ADJ_TITLES = {"bonus": "Премия", "penalty": "Штраф", "deduction": "Удержание"}
ADJ_STATUS_TITLES = {
    "pending": "ожидает подписи",
    "awaiting_consent": "ожидает согласия сотрудника",
    "approved": "подписано и проведено",
    "rejected": "отклонено",
    "disputed": "сотрудник не согласен",
    "cancelled": "отменено",
}
PAYOUT_KIND_TITLES = {"advance": "аванс", "salary": "зарплата", "on_demand": "по запросу"}
PAYOUT_STATUS_TITLES = {"registered": "ожидает подтверждения", "confirmed": "проведена", "cancelled": "отменена"}
LEDGER_TITLES = {
    "work": "Работа",
    "bonus": "Премия",
    "penalty": "Штраф",
    "deduction": "Удержание",
    "payout": "Выплата",
    "reversal": "Сторно",
}


class PendingEntriesError(DomainError):
    def __init__(self, count: int):
        super().__init__(f"В периоде {count} неподтверждённых операций")
        self.count = count


async def _currency(session: AsyncSession, code: str) -> Currency:
    currency = await session.get(Currency, code.upper())
    if currency is None:
        raise ValidationError(f"Нет валюты {code}")
    return currency


async def _check_amount(session: AsyncSession, amount: Decimal, currency_code: str) -> Currency:
    currency = await _currency(session, currency_code)
    if amount <= 0:
        raise ValidationError("Сумма должна быть больше нуля")
    if scale_of(amount) > currency.minor_units:
        raise ValidationError(f"{currency.code}: не больше {currency.minor_units} знаков после запятой")
    return currency


async def _department_for(session: AsyncSession, employee_id: int, department_id: int | None) -> int | None:
    if department_id is not None:
        return department_id
    return await org.default_department_id(session, employee_id)


# ------------------------------------------------------------------ премии, штрафы, удержания


async def create_adjustment(
    session: AsyncSession,
    actor,
    settings: Settings,
    *,
    kind: str,
    employee: Employee,
    amount: Decimal,
    currency: str | None,
    reason: str,
    idempotency_key: str,
    department_id: int | None = None,
    business_date: dt.date | None = None,
    basis_type: str | None = None,
    basis_id: int | None = None,
    check_permission: bool = True,
) -> tuple[Adjustment, bool]:
    existing = (
        await session.execute(select(Adjustment).where(Adjustment.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing:
        return existing, False
    if kind not in ADJ_TITLES:
        raise ValidationError("Тип: bonus, penalty, deduction")
    department_id = await _department_for(session, employee.id, department_id)
    if check_permission:
        actor.require(Perm.PAYROLL_ADJUST, department_id)
    if not reason or not reason.strip():
        raise ValidationError("Укажите основание")
    currency_code = (currency or settings.currency).upper()
    await _check_amount(session, amount, currency_code)
    stmt = (
        insert(Adjustment)
        .values(
            idempotency_key=idempotency_key,
            kind=kind,
            employee_id=employee.id,
            department_id=department_id,
            amount=amount,
            currency=currency_code,
            reason=reason.strip(),
            business_date=business_date or settings.today(),
            basis_type=basis_type,
            basis_id=basis_id,
            status="pending",
            created_by=actor.user.id,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(Adjustment.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is None:
        return (
            await session.execute(select(Adjustment).where(Adjustment.idempotency_key == idempotency_key))
        ).scalar_one(), False
    adj = await session.get(Adjustment, new_id)
    await audit(
        session,
        actor.user.id,
        f"payroll.{kind}_create",
        "adjustment",
        adj.id,
        {"employee_id": employee.id, "amount": amount, "currency": currency_code, "reason": reason},
    )
    return adj, True


async def _locked_adj(session: AsyncSession, adj_id: int) -> Adjustment:
    adj = await session.get(Adjustment, adj_id, with_for_update=True, populate_existing=True)
    if adj is None:
        raise NotFound(f"Документ #{adj_id} не найден")
    return adj


async def _post_adjustment(
    session: AsyncSession, settings: Settings, adj: Adjustment, user_id: int
) -> LedgerEntry | None:
    """Премия проводится целиком. Штраф/удержание — через удержания частями с учётом лимита."""
    adj.status = "approved"
    if adj.kind != "bonus":
        await session.flush()
        posted = await apply_withholdings(session, settings, adj.employee_id, adj.currency, user_id)
        return next((entry for entry, adj_id in posted if adj_id == adj.id), None)
    entry, _ = await ledger.post(
        session,
        settings,
        employee_id=adj.employee_id,
        department_id=adj.department_id,
        currency=adj.currency,
        amount=adj.amount,
        entry_type=adj.kind,
        source_type="adjustment",
        source_id=adj.id,
        business_date=adj.business_date,
        posted_by=user_id,
        comment=f"{adj.number}: {adj.reason}",
    )
    await session.flush()
    # Премия увеличивает начисления периода — при лимите удержаний может освободиться место.
    await apply_withholdings(session, settings, adj.employee_id, adj.currency, user_id)
    return entry


async def _period_withholding_room(
    session: AsyncSession, settings: Settings, employee_id: int, currency: str, period: PayrollPeriod
) -> Decimal | None:
    """Сколько ещё можно удержать в периоде: лимит% × начисления периода − уже удержанное. None — без лимита."""
    limit = settings.deduction_limit_percent
    if limit is None:
        return None
    original = aliased(LedgerEntry)
    rows = (
        await session.execute(
            select(LedgerEntry.entry_type, original.entry_type, func.sum(LedgerEntry.amount))
            .outerjoin(original, original.id == LedgerEntry.reverses_id)
            .where(
                LedgerEntry.employee_id == employee_id,
                LedgerEntry.currency == currency,
                LedgerEntry.period_id == period.id,
            )
            .group_by(LedgerEntry.entry_type, original.entry_type)
        )
    ).all()
    base = used = Decimal(0)
    for entry_type, original_type, total in rows:
        effective = original_type if entry_type == "reversal" else entry_type
        if effective in ("work", "bonus"):
            base += total
        elif effective in ("penalty", "deduction"):
            used -= total
    cur = await _currency(session, currency)
    cap = round_to_step(base * limit / Decimal(100), currency_step(cur.minor_units), "DOWN") if base > 0 else 0
    return cap - used


async def apply_withholdings(
    session: AsyncSession, settings: Settings, employee_id: int, currency: str, user_id: int | None
) -> list[tuple[LedgerEntry, int]]:
    """Проводит очередные части подписанных штрафов/удержаний (старые — первыми) в пределах лимита.

    Без лимита удержание проводится целиком сразу. С лимитом — в текущем открытом периоде не больше
    deduction_limit_percent от начислений периода; остаток ждёт новых начислений.
    """
    pending = select(Adjustment.id).where(
        Adjustment.employee_id == employee_id,
        Adjustment.currency == currency,
        Adjustment.status == "approved",
        Adjustment.kind != "bonus",
        Adjustment.withheld < Adjustment.amount,
    )
    if (await session.execute(pending.limit(1))).first() is None:
        return []
    # Сериализуем удержания по сотруднику: параллельные начисления не должны удержать дважды.
    await session.execute(select(Employee.id).where(Employee.id == employee_id).with_for_update())
    adjustments = (
        (
            await session.execute(
                select(Adjustment)
                .where(Adjustment.id.in_(pending))
                .order_by(Adjustment.business_date, Adjustment.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    limited = settings.deduction_limit_percent is not None
    today = settings.today()
    posted: list[tuple[LedgerEntry, int]] = []
    for adj in adjustments:
        business_date = today if limited else adj.business_date
        period = await ledger.posting_period(session, business_date, settings)
        room = await _period_withholding_room(session, settings, employee_id, currency, period)
        take = adj.outstanding if room is None else min(adj.outstanding, room)
        if take <= 0:
            break
        seq = (
            await session.execute(
                select(func.count())
                .select_from(AdjustmentInstallment)
                .where(AdjustmentInstallment.adjustment_id == adj.id)
            )
        ).scalar_one() + 1
        installment = AdjustmentInstallment(adjustment_id=adj.id, seq=seq, period_id=period.id, amount=take)
        session.add(installment)
        await session.flush()
        part = "" if take == adj.amount else f" (часть {seq}: {take.normalize():f} из {adj.amount.normalize():f})"
        entry, _ = await ledger.post(
            session,
            settings,
            employee_id=adj.employee_id,
            department_id=adj.department_id,
            currency=adj.currency,
            amount=-take,
            entry_type=adj.kind,
            source_type="installment",
            source_id=installment.id,
            business_date=business_date,
            posted_by=user_id,
            comment=f"{adj.number}: {adj.reason}{part}",
        )
        adj.withheld = adj.withheld + take
        posted.append((entry, adj.id))
        await audit(
            session,
            user_id,
            "payroll.withhold",
            "adjustment",
            adj.id,
            {"installment": seq, "amount": take, "period_id": period.id, "outstanding": adj.outstanding},
        )
    await session.flush()
    return posted


async def outstanding_withholdings(session: AsyncSession, employee_id: int) -> dict[str, Decimal]:
    rows = (
        await session.execute(
            select(Adjustment.currency, func.sum(Adjustment.amount - Adjustment.withheld))
            .where(
                Adjustment.employee_id == employee_id,
                Adjustment.status == "approved",
                Adjustment.kind != "bonus",
                Adjustment.withheld < Adjustment.amount,
            )
            .group_by(Adjustment.currency)
        )
    ).all()
    return {code: total for code, total in rows}


async def sign_adjustment(
    session: AsyncSession, actor, settings: Settings, adj_id: int
) -> tuple[Adjustment, LedgerEntry | None, bool]:
    """Подпись. Возвращает (документ, проводка, изменилось ли что-то)."""
    adj = await _locked_adj(session, adj_id)
    if adj.status in ("approved", "awaiting_consent"):
        return adj, None, False
    if adj.status != "pending":
        raise Conflict(f"{adj.number}: подписать нельзя, статус {ADJ_STATUS_TITLES[adj.status]}")
    actor.require(Perm.PAYROLL_SIGN, adj.department_id)
    if not actor.is_owner:
        if actor.is_self(adj.employee_id):
            raise AccessDenied("Нельзя подписывать документы в отношении себя")
        if adj.created_by == actor.user.id and not settings.allow_self_confirm:
            raise AccessDenied("Автор не может сам подписать документ (настройка allow_self_confirm)")
    adj.decided_by = actor.user.id
    adj.decided_at = func.now()
    posted = None
    if adj.kind != "bonus" and settings.deduction_requires_consent:
        adj.status = "awaiting_consent"
    else:
        posted = await _post_adjustment(session, settings, adj, actor.user.id)
    await audit(session, actor.user.id, "payroll.sign", "adjustment", adj.id, {"status": adj.status})
    return adj, posted, True


async def consent_adjustment(
    session: AsyncSession, actor, settings: Settings, adj_id: int, agree: bool
) -> tuple[Adjustment, LedgerEntry | None]:
    adj = await _locked_adj(session, adj_id)
    if not actor.is_self(adj.employee_id):
        raise AccessDenied("Согласие даёт только сам сотрудник")
    if adj.status != "awaiting_consent":
        if adj.status in ("approved", "disputed"):
            return adj, None
        raise Conflict(f"{adj.number}: {ADJ_STATUS_TITLES[adj.status]}")
    posted = None
    if agree:
        adj.consent_at = func.now()
        posted = await _post_adjustment(session, settings, adj, actor.user.id)
    else:
        adj.status = "disputed"
    await audit(session, actor.user.id, "payroll.consent", "adjustment", adj.id, {"agree": agree})
    return adj, posted


async def reject_adjustment(session: AsyncSession, actor, adj_id: int, comment: str | None) -> Adjustment:
    adj = await _locked_adj(session, adj_id)
    if adj.status == "rejected":
        return adj
    if adj.status not in ("pending", "awaiting_consent", "disputed"):
        raise Conflict(f"{adj.number}: {ADJ_STATUS_TITLES[adj.status]}")
    actor.require(Perm.PAYROLL_SIGN, adj.department_id)
    adj.status = "rejected"
    adj.decided_by = actor.user.id
    adj.decided_at = func.now()
    adj.decision_comment = comment
    await audit(session, actor.user.id, "payroll.reject", "adjustment", adj.id, {"comment": comment})
    return adj


async def cancel_adjustment(
    session: AsyncSession, actor, settings: Settings, adj_id: int, reason: str
) -> tuple[Adjustment, LedgerEntry | None]:
    adj = await _locked_adj(session, adj_id)
    if adj.status == "cancelled":
        return adj, None
    if not reason or not reason.strip():
        raise ValidationError("Укажите причину отмены")
    reversal = None
    if adj.status == "approved":
        actor.require(Perm.PAYROLL_SIGN, adj.department_id)
        if actor.is_self(adj.employee_id) and not actor.is_owner:
            raise AccessDenied("Нельзя отменять документы в отношении себя")
        originals = []
        legacy = await ledger.find_entry(session, "adjustment", adj.id, adj.kind)
        if legacy:
            originals.append(legacy)
        installment_ids = select(AdjustmentInstallment.id).where(AdjustmentInstallment.adjustment_id == adj.id)
        originals += (
            (
                await session.execute(
                    select(LedgerEntry).where(
                        LedgerEntry.source_type == "installment",
                        LedgerEntry.source_id.in_(installment_ids),
                        LedgerEntry.entry_type == adj.kind,
                    )
                )
            )
            .scalars()
            .all()
        )
        for original in originals:
            entry = await ledger.reverse(
                session, settings, original, posted_by=actor.user.id, comment=f"Отмена {adj.number}: {reason}"
            )
            reversal = reversal or entry
    elif adj.status in ("pending", "awaiting_consent", "disputed"):
        actor.require(Perm.PAYROLL_ADJUST, adj.department_id)
    else:
        raise Conflict(f"{adj.number}: {ADJ_STATUS_TITLES[adj.status]}")
    adj.status = "cancelled"
    adj.decision_comment = reason
    await audit(session, actor.user.id, "payroll.adjustment_cancel", "adjustment", adj.id, {"reason": reason})
    return adj, reversal


async def get_adjustment(session: AsyncSession, adj_id: int) -> Adjustment:
    adj = await session.get(Adjustment, adj_id)
    if adj is None:
        raise NotFound(f"Документ #{adj_id} не найден")
    return adj


async def open_adjustments(session: AsyncSession, actor) -> list[tuple[Adjustment, Employee]]:
    depts = actor.departments_with(Perm.PAYROLL_SIGN)
    query = (
        select(Adjustment, Employee)
        .join(Employee, Employee.id == Adjustment.employee_id)
        .where(Adjustment.status.in_(("pending", "awaiting_consent", "disputed")))
        .order_by(Adjustment.id)
    )
    if depts is not None:
        query = query.where(Adjustment.department_id.in_(depts or {-1}))
    return [tuple(r) for r in (await session.execute(query)).all()]


# ------------------------------------------------------------------ выплаты


async def register_payout(
    session: AsyncSession,
    actor,
    settings: Settings,
    *,
    employee: Employee,
    amount: Decimal,
    currency: str | None,
    kind: str,
    idempotency_key: str,
    method: str | None = None,
    comment: str | None = None,
    department_id: int | None = None,
    request_id: int | None = None,
    business_date: dt.date | None = None,
) -> tuple[Payout, bool]:
    existing = (
        await session.execute(select(Payout).where(Payout.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing:
        return existing, False
    if kind not in PAYOUT_KIND_TITLES:
        raise ValidationError("Вид выплаты: advance (аванс), salary (зарплата), on_demand (по запросу)")
    department_id = await _department_for(session, employee.id, department_id)
    actor.require(Perm.PAYROLL_PAYOUT, department_id)
    if actor.is_self(employee.id) and not actor.is_owner:
        raise AccessDenied("Нельзя регистрировать выплату самому себе")
    currency_code = (currency or settings.currency).upper()
    cur = await _check_amount(session, amount, currency_code)
    # Блокируем строку сотрудника, чтобы две параллельные выплаты не прошли проверку остатка одновременно.
    await session.execute(select(Employee.id).where(Employee.id == employee.id).with_for_update())
    can_pay = await ledger.available(session, employee.id, currency_code, exclude_request_id=request_id)
    credit = Decimal(0) if kind == "salary" else await credit_limit(session, settings, employee.id, currency_code)
    if amount > can_pay + credit:
        raise InsufficientFunds(_funds_message(can_pay, credit, currency_code, cur.minor_units, kind))
    stmt = (
        insert(Payout)
        .values(
            idempotency_key=idempotency_key,
            employee_id=employee.id,
            department_id=department_id,
            amount=amount,
            currency=currency_code,
            kind=kind,
            method=method,
            comment=comment,
            status="registered",
            business_date=business_date or settings.today(),
            request_id=request_id,
            registered_by=actor.user.id,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(Payout.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is None:
        return (
            await session.execute(select(Payout).where(Payout.idempotency_key == idempotency_key))
        ).scalar_one(), False
    payout = await session.get(Payout, new_id)
    await audit(
        session,
        actor.user.id,
        "payroll.payout_register",
        "payout",
        payout.id,
        {"employee_id": employee.id, "amount": amount, "currency": currency_code, "kind": kind},
    )
    if not settings.payout_requires_confirmation:
        await _confirm_payout(session, settings, payout, actor.user.id)
    return payout, True


async def _confirm_payout(session: AsyncSession, settings: Settings, payout: Payout, user_id: int) -> LedgerEntry:
    entry, _ = await ledger.post(
        session,
        settings,
        employee_id=payout.employee_id,
        department_id=payout.department_id,
        currency=payout.currency,
        amount=-payout.amount,
        entry_type="payout",
        source_type="payout",
        source_id=payout.id,
        business_date=payout.business_date,
        posted_by=user_id,
        comment=f"{payout.number} ({PAYOUT_KIND_TITLES[payout.kind]})"
        + (f": {payout.comment}" if payout.comment else ""),
    )
    payout.status = "confirmed"
    payout.confirmed_by = user_id
    payout.confirmed_at = func.now()
    await audit(session, user_id, "payroll.payout_confirm", "payout", payout.id)
    return entry


async def _locked_payout(session: AsyncSession, payout_id: int) -> Payout:
    payout = await session.get(Payout, payout_id, with_for_update=True, populate_existing=True)
    if payout is None:
        raise NotFound(f"Выплата #{payout_id} не найдена")
    return payout


async def confirm_payout(
    session: AsyncSession, actor, settings: Settings, payout_id: int
) -> tuple[Payout, LedgerEntry | None, bool]:
    payout = await _locked_payout(session, payout_id)
    if payout.status == "confirmed":
        return payout, None, False
    if payout.status != "registered":
        raise Conflict(f"{payout.number}: {PAYOUT_STATUS_TITLES[payout.status]}")
    actor.require(Perm.PAYROLL_PAYOUT_CONFIRM, payout.department_id)
    if not actor.is_owner:
        if settings.payout_confirm_owner_only:
            raise AccessDenied("Выплату подтверждает только владелец. Изменить: Управление, Ещё, Настройки")
        if actor.is_self(payout.employee_id):
            raise AccessDenied("Нельзя подтверждать выплату самому себе")
        if payout.registered_by == actor.user.id and not settings.allow_self_confirm:
            raise AccessDenied("Регистрировал и подтверждает один человек (настройка allow_self_confirm)")
    entry = await _confirm_payout(session, settings, payout, actor.user.id)
    return payout, entry, True


async def cancel_payout(
    session: AsyncSession, actor, settings: Settings, payout_id: int, reason: str
) -> tuple[Payout, LedgerEntry | None]:
    payout = await _locked_payout(session, payout_id)
    if payout.status == "cancelled":
        return payout, None
    if not reason or not reason.strip():
        raise ValidationError("Укажите причину отмены выплаты")
    reversal = None
    if payout.status == "confirmed":
        actor.require(Perm.PAYROLL_PAYOUT_CONFIRM, payout.department_id)
        original = await ledger.find_entry(session, "payout", payout.id, "payout")
        if original:
            reversal = await ledger.reverse(
                session, settings, original, posted_by=actor.user.id, comment=f"Отмена {payout.number}: {reason}"
            )
    else:
        actor.require(Perm.PAYROLL_PAYOUT, payout.department_id)
    payout.status = "cancelled"
    payout.cancelled_by = actor.user.id
    payout.cancelled_at = func.now()
    payout.cancel_reason = reason
    await audit(session, actor.user.id, "payroll.payout_cancel", "payout", payout.id, {"reason": reason})
    return payout, reversal


async def registered_payouts(session: AsyncSession, actor) -> list[tuple[Payout, Employee]]:
    depts = actor.departments_with(Perm.PAYROLL_PAYOUT_CONFIRM)
    query = (
        select(Payout, Employee)
        .join(Employee, Employee.id == Payout.employee_id)
        .where(Payout.status == "registered")
        .order_by(Payout.id)
    )
    if depts is not None:
        query = query.where(Payout.department_id.in_(depts or {-1}))
    return [tuple(r) for r in (await session.execute(query)).all()]


def _funds_message(can_pay: Decimal, credit: Decimal, currency: str, minor: int, kind: str) -> str:
    text = f"Доступно к выплате {fmt_money(max(can_pay, Decimal(0)), currency, minor)}"
    if can_pay < 0:
        text += f" (долг {fmt_money(-can_pay, currency, minor)})"
    if kind == "salary":
        return text + ". Сверх заработанного можно выдать только аванс или выплату по запросу"
    if credit > 0:
        return text + f", аванс в долг: до {fmt_money(credit, currency, minor)} сверх этого"
    return text + ". Аванс в долг сотруднику не разрешён: включите его в карточке сотрудника"


async def credit_limit(session: AsyncSession, settings: Settings, employee_id: int, currency: str) -> Decimal:
    """Сколько можно выдать сверх заработанного: индивидуальный лимит или default_credit_limit."""
    row = await session.get(EmployeeCreditLimit, (employee_id, currency))
    return row.amount if row is not None else settings.default_credit_limit


async def set_credit_limit(
    session: AsyncSession, actor, settings: Settings, employee: Employee, amount: Decimal, currency: str | None
) -> EmployeeCreditLimit:
    department_id = await org.default_department_id(session, employee.id)
    actor.require(Perm.PAYROLL_SIGN, department_id)
    if actor.is_self(employee.id) and not actor.is_owner:
        raise AccessDenied("Нельзя менять кредитный лимит себе")
    currency_code = (currency or settings.currency).upper()
    cur = await _currency(session, currency_code)
    if amount < 0 or scale_of(amount) > cur.minor_units:
        raise ValidationError("Лимит должен быть неотрицательной суммой в валюте")
    stmt = insert(EmployeeCreditLimit).values(
        employee_id=employee.id, currency=currency_code, amount=amount, set_by=actor.user.id
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["employee_id", "currency"],
        set_={"amount": amount, "set_by": actor.user.id, "set_at": func.now()},
    )
    await session.execute(stmt)
    await audit(
        session,
        actor.user.id,
        "payroll.credit_limit",
        "employee",
        employee.id,
        {"amount": amount, "currency": currency_code},
    )
    return await session.get(EmployeeCreditLimit, (employee.id, currency_code), populate_existing=True)


async def list_credit_limits(session: AsyncSession) -> list[tuple[EmployeeCreditLimit, Employee]]:
    rows = (
        await session.execute(
            select(EmployeeCreditLimit, Employee)
            .join(Employee, Employee.id == EmployeeCreditLimit.employee_id)
            .order_by(Employee.full_name)
        )
    ).all()
    return [tuple(r) for r in rows]


# ------------------------------------------------------------------ запросы выплат


async def request_payout(
    session: AsyncSession,
    actor,
    settings: Settings,
    *,
    amount: Decimal,
    currency: str | None,
    comment: str | None,
    idempotency_key: str,
) -> tuple[PayoutRequest, bool]:
    if actor.employee is None:
        raise AccessDenied("Ваш Telegram не привязан к карточке сотрудника")
    existing = (
        await session.execute(select(PayoutRequest).where(PayoutRequest.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing:
        return existing, False
    employee = actor.employee
    currency_code = (currency or settings.currency).upper()
    cur = await _check_amount(session, amount, currency_code)
    await session.execute(select(Employee.id).where(Employee.id == employee.id).with_for_update())
    can_pay = await ledger.available(session, employee.id, currency_code)
    credit = await credit_limit(session, settings, employee.id, currency_code)
    if amount > can_pay + credit:
        raise InsufficientFunds(_funds_message(can_pay, credit, currency_code, cur.minor_units, "on_demand"))
    req = PayoutRequest(
        idempotency_key=idempotency_key,
        employee_id=employee.id,
        amount=amount,
        currency=currency_code,
        comment=comment,
        status="requested",
        created_by=actor.user.id,
    )
    session.add(req)
    await session.flush()
    await audit(session, actor.user.id, "payroll.request", "payout_request", req.id, {"amount": amount})
    return req, True


async def decide_request(
    session: AsyncSession, actor, settings: Settings, request_id: int, approve: bool, comment: str | None = None
) -> tuple[PayoutRequest, Payout | None]:
    req = await session.get(PayoutRequest, request_id, with_for_update=True, populate_existing=True)
    if req is None:
        raise NotFound(f"Запрос #{request_id} не найден")
    if req.status != "requested":
        payout = await session.get(Payout, req.payout_id) if req.payout_id else None
        return req, payout
    employee = await session.get(Employee, req.employee_id)
    department_id = await org.default_department_id(session, employee.id)
    actor.require(Perm.PAYROLL_PAYOUT, department_id)
    payout = None
    if approve:
        payout, _ = await register_payout(
            session,
            actor,
            settings,
            employee=employee,
            amount=req.amount,
            currency=req.currency,
            kind="on_demand",
            idempotency_key=f"request:{req.id}",
            comment=req.comment,
            request_id=req.id,
        )
        req.status = "approved"
        req.payout_id = payout.id
    else:
        req.status = "rejected"
    req.decided_by = actor.user.id
    req.decided_at = func.now()
    req.decision_comment = comment
    await audit(session, actor.user.id, "payroll.request_decide", "payout_request", req.id, {"approve": approve})
    return req, payout


async def cancel_request(session: AsyncSession, actor, request_id: int) -> PayoutRequest:
    req = await session.get(PayoutRequest, request_id, with_for_update=True)
    if req is None or not actor.is_self(req.employee_id):
        raise NotFound("Запрос не найден")
    if req.status != "requested":
        raise Conflict("Запрос уже рассмотрен")
    req.status = "cancelled"
    await audit(session, actor.user.id, "payroll.request_cancel", "payout_request", req.id)
    return req


async def open_requests(session: AsyncSession, actor) -> list[tuple[PayoutRequest, Employee]]:
    rows = (
        await session.execute(
            select(PayoutRequest, Employee)
            .join(Employee, Employee.id == PayoutRequest.employee_id)
            .where(PayoutRequest.status == "requested")
            .order_by(PayoutRequest.id)
        )
    ).all()
    result = []
    for req, emp in rows:
        dept = await org.default_department_id(session, emp.id)
        if actor.can(Perm.PAYROLL_PAYOUT, dept):
            result.append((req, emp))
    return result


# ------------------------------------------------------------------ периоды


async def get_period(session: AsyncSession, period_id: int) -> PayrollPeriod:
    period = await session.get(PayrollPeriod, period_id)
    if period is None:
        raise NotFound(f"Период #{period_id} не найден")
    return period


async def list_periods(session: AsyncSession, limit: int = 12) -> list[PayrollPeriod]:
    return list(
        (await session.execute(select(PayrollPeriod).order_by(PayrollPeriod.date_from.desc()).limit(limit))).scalars()
    )


BUCKETS = {
    "work": "accrued",
    "bonus": "accrued",
    "penalty": "deducted",
    "deduction": "deducted",
    "payout": "paid",
    "reversal": "corrections",
}


async def close_period(
    session: AsyncSession, actor, settings: Settings, period_id: int, force: bool = False
) -> tuple[PayrollPeriod, list[PeriodStatement]]:
    actor.require(Perm.PERIOD_CLOSE)
    period = await session.get(PayrollPeriod, period_id, with_for_update=True, populate_existing=True)
    if period is None:
        raise NotFound(f"Период #{period_id} не найден")
    if period.status == "closed":
        raise Conflict(f"Период {period.title} уже закрыт")
    if period.date_to >= settings.today() and not actor.is_owner:
        raise ValidationError(f"Период {period.title} ещё не закончился")
    earlier_open = (
        await session.execute(
            select(func.count())
            .select_from(PayrollPeriod)
            .where(PayrollPeriod.status == "open", PayrollPeriod.date_to < period.date_from)
        )
    ).scalar_one()
    if earlier_open:
        raise Conflict("Сначала закройте более ранние периоды: /periods")
    pending = (
        await session.execute(
            select(func.count())
            .select_from(WorkEntry)
            .where(
                WorkEntry.status.in_(("draft", "pending")),
                WorkEntry.performed_on.between(period.date_from, period.date_to),
            )
        )
    ).scalar_one()
    if pending and not force:
        raise PendingEntriesError(pending)

    version = period.version + 1
    statements = await _build_period_statements(session, period, version)
    for st in statements:
        session.add(st)
    period.status = "closed"
    period.version = version
    period.closed_by = actor.user.id
    period.closed_at = func.now()
    await session.flush()
    await audit(
        session,
        actor.user.id,
        "period.close",
        "payroll_period",
        period.id,
        {"version": version, "statements": len(statements), "pending_left": pending},
    )
    return period, statements


async def _build_period_statements(session: AsyncSession, period: PayrollPeriod, version: int) -> list[PeriodStatement]:
    rows = (
        await session.execute(
            select(
                LedgerEntry.id,
                LedgerEntry.employee_id,
                LedgerEntry.currency,
                LedgerEntry.amount,
                LedgerEntry.entry_type,
                PayrollPeriod.date_from,
            )
            .join(PayrollPeriod, PayrollPeriod.id == LedgerEntry.period_id)
            .where(PayrollPeriod.date_from <= period.date_from)
            .order_by(LedgerEntry.id)
        )
    ).all()
    acc: dict[tuple[int, str], dict] = defaultdict(
        lambda: {
            "opening": Decimal(0),
            "accrued": Decimal(0),
            "deducted": Decimal(0),
            "corrections": Decimal(0),
            "paid": Decimal(0),
            "count": 0,
            "hash": hashlib.sha256(),
        }
    )
    for row in rows:
        bucket = acc[(row.employee_id, row.currency)]
        if row.date_from < period.date_from:
            bucket["opening"] += row.amount
        else:
            bucket[BUCKETS[row.entry_type]] += row.amount
            bucket["count"] += 1
            bucket["hash"].update(f"{row.id}:{row.amount};".encode())
    result = []
    for (employee_id, currency), b in sorted(acc.items()):
        closing = b["opening"] + b["accrued"] + b["deducted"] + b["corrections"] + b["paid"]
        result.append(
            PeriodStatement(
                period_id=period.id,
                version=version,
                employee_id=employee_id,
                currency=currency,
                opening=b["opening"],
                accrued=b["accrued"],
                deducted=b["deducted"],
                corrections=b["corrections"],
                paid=b["paid"],
                closing=closing,
                entries_count=b["count"],
                entries_hash=b["hash"].hexdigest(),
            )
        )
    return result


async def reopen_period(session: AsyncSession, actor, period_id: int, reason: str) -> PayrollPeriod:
    if not actor.is_owner or not actor.can(Perm.PERIOD_REOPEN):
        raise AccessDenied("Переоткрыть период может только владелец")
    if not reason or len(reason.strip()) < 3:
        raise ValidationError("Укажите причину переоткрытия")
    period = await session.get(PayrollPeriod, period_id, with_for_update=True, populate_existing=True)
    if period is None:
        raise NotFound(f"Период #{period_id} не найден")
    if period.status == "open":
        raise Conflict("Период и так открыт")
    later_closed = (
        await session.execute(
            select(func.count())
            .select_from(PayrollPeriod)
            .where(PayrollPeriod.status == "closed", PayrollPeriod.date_from > period.date_to)
        )
    ).scalar_one()
    if later_closed:
        raise Conflict("Сначала переоткройте более поздние закрытые периоды, иначе их входящие остатки разойдутся")
    period.status = "open"
    session.add(PeriodReopening(period_id=period.id, reopened_by=actor.user.id, reason=reason.strip()))
    await audit(session, actor.user.id, "period.reopen", "payroll_period", period.id, {"reason": reason})
    return period


async def period_statements(session: AsyncSession, period_id: int, version: int | None = None):
    period = await get_period(session, period_id)
    version = version or period.version
    rows = (
        await session.execute(
            select(PeriodStatement, Employee)
            .join(Employee, Employee.id == PeriodStatement.employee_id)
            .where(PeriodStatement.period_id == period_id, PeriodStatement.version == version)
            .order_by(Employee.full_name)
        )
    ).all()
    return period, [tuple(r) for r in rows]


# ------------------------------------------------------------------ подробный расчёт


@dataclass
class WorkLine:
    title: str
    unit: str
    rate: Decimal
    count: int
    quantity: Decimal
    amount: Decimal
    method: str = "per_unit"
    coefficient: Decimal = Decimal(1)
    tiered: bool = False


@dataclass
class OtherLine:
    entry_type: str
    business_date: dt.date
    amount: Decimal
    comment: str


@dataclass
class CurrencyStatement:
    currency: str
    minor_units: int
    opening: Decimal = Decimal(0)
    accrued: Decimal = Decimal(0)
    deducted: Decimal = Decimal(0)
    corrections: Decimal = Decimal(0)
    paid: Decimal = Decimal(0)
    pending_amount: Decimal = Decimal(0)
    pending_count: int = 0
    reserved: Decimal = Decimal(0)
    # Подписанные штрафы/удержания, которые ещё будут удержаны (лимит удержаний).
    outstanding_withholding: Decimal = Decimal(0)
    # Сколько можно выдать сверх заработанного (аванс в долг).
    credit_limit: Decimal = Decimal(0)
    work_lines: list[WorkLine] = field(default_factory=list)
    other_lines: list[OtherLine] = field(default_factory=list)

    @property
    def closing(self) -> Decimal:
        return self.opening + self.accrued + self.deducted + self.corrections + self.paid

    @property
    def available(self) -> Decimal:
        return self.closing - self.reserved


@dataclass
class Statement:
    employee: Employee
    title: str
    date_from: dt.date
    date_to: dt.date
    period: PayrollPeriod | None
    currencies: dict[str, CurrencyStatement]


async def build_statement(
    session: AsyncSession,
    employee: Employee,
    *,
    period: PayrollPeriod | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    include_reserved: bool = False,
) -> Statement:
    """Расчёт за период (по проводкам периода) или за произвольные даты (по дате операции)."""
    if period is not None:
        date_from, date_to = period.date_from, period.date_to
        in_scope = LedgerEntry.period_id == period.id
        before = (
            select(LedgerEntry.currency, func.sum(LedgerEntry.amount))
            .join(PayrollPeriod, PayrollPeriod.id == LedgerEntry.period_id)
            .where(LedgerEntry.employee_id == employee.id, PayrollPeriod.date_from < period.date_from)
            .group_by(LedgerEntry.currency)
        )
        title = f"Период {period.title}" + (" (закрыт)" if period.status == "closed" else "")
    else:
        assert date_from is not None and date_to is not None
        in_scope = LedgerEntry.business_date.between(date_from, date_to)
        before = (
            select(LedgerEntry.currency, func.sum(LedgerEntry.amount))
            .where(LedgerEntry.employee_id == employee.id, LedgerEntry.business_date < date_from)
            .group_by(LedgerEntry.currency)
        )
        title = f"{date_from:%d.%m.%Y}-{date_to:%d.%m.%Y}"

    currencies: dict[str, CurrencyStatement] = {}
    minor = dict((await session.execute(select(Currency.code, Currency.minor_units))).all())

    def cs(code: str) -> CurrencyStatement:
        if code not in currencies:
            currencies[code] = CurrencyStatement(currency=code, minor_units=minor.get(code, 2))
        return currencies[code]

    for code, total in (await session.execute(before)).all():
        cs(code).opening = total

    work_rows = (
        await session.execute(
            select(WorkType.name, WorkType.calc_method, WorkEntry, LedgerEntry.currency, LedgerEntry.amount)
            .join(WorkEntry, and_(LedgerEntry.source_type == "work_entry", WorkEntry.id == LedgerEntry.source_id))
            .join(WorkType, WorkType.id == WorkEntry.work_type_id)
            .where(LedgerEntry.employee_id == employee.id, LedgerEntry.entry_type == "work", in_scope)
            .order_by(WorkType.name, WorkEntry.id)
        )
    ).all()
    # Строки расшифровки: вид работ × ставка (по ступеням — отдельной строкой на ступень) × коэффициент.
    grouped: dict[tuple, WorkLine] = {}
    for name, method, entry, code, amount in work_rows:
        st = cs(code)
        st.accrued += amount
        details = entry.calc_details or {}
        pieces = details.get("pieces") or [{"qty": str(entry.quantity), "rate": str(entry.rate), "amount": str(amount)}]
        coefficient = Decimal(details.get("coefficient", "1"))
        tiered = bool(details.get("tiered"))
        for piece in pieces:
            rate = Decimal(piece["rate"])
            key = (code, name, entry.unit_code, rate, method, coefficient, tiered)
            line = grouped.get(key)
            if line is None:
                line = WorkLine(name, entry.unit_code, rate, 0, Decimal(0), Decimal(0), method, coefficient, tiered)
                grouped[key] = line
                st.work_lines.append(line)
            line.count += 1
            line.quantity += Decimal(piece["qty"])
            line.amount += Decimal(piece["amount"])

    other_rows = (
        await session.execute(
            select(LedgerEntry)
            .where(LedgerEntry.employee_id == employee.id, LedgerEntry.entry_type != "work", in_scope)
            .order_by(LedgerEntry.business_date, LedgerEntry.id)
        )
    ).scalars()
    for row in other_rows:
        st = cs(row.currency)
        st.other_lines.append(OtherLine(row.entry_type, row.business_date, row.amount, row.comment or ""))
        bucket = BUCKETS[row.entry_type]
        setattr(st, bucket, getattr(st, bucket) + row.amount)

    pending_rows = (
        await session.execute(
            select(WorkEntry.currency, func.count(), func.sum(WorkEntry.amount))
            .where(
                WorkEntry.employee_id == employee.id,
                WorkEntry.status == "pending",
                WorkEntry.performed_on.between(date_from, date_to),
            )
            .group_by(WorkEntry.currency)
        )
    ).all()
    for code, count, total in pending_rows:
        st = cs(code)
        st.pending_count, st.pending_amount = count, total

    if include_reserved:
        for code, amount in (await ledger.reserved(session, employee.id)).items():
            cs(code).reserved = amount
        for code, amount in (await outstanding_withholdings(session, employee.id)).items():
            cs(code).outstanding_withholding = amount
        settings_row = await get_settings(session)
        for code, st in currencies.items():
            st.credit_limit = await credit_limit(session, settings_row, employee.id, code)

    return Statement(employee, title, date_from, date_to, period, currencies)


async def employee_payouts(session: AsyncSession, employee_id: int, limit: int = 20) -> list[Payout]:
    return list(
        (
            await session.execute(
                select(Payout).where(Payout.employee_id == employee_id).order_by(Payout.id.desc()).limit(limit)
            )
        ).scalars()
    )
