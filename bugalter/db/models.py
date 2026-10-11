"""Модели базы данных.

Принципы:
* деньги и количества — Numeric (Decimal в Python);
* журналы (ledger_entries, stock_moves, tariffs, audit_log, period_statements) — только добавление,
  UPDATE/DELETE запрещены триггерами (см. миграцию 0001);
* повторные запросы гасятся уникальными ключами (idempotency_key, источник проводки).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import MONEY, QTY, RATE, Base


def _now_col() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _user_fk(nullable: bool = True) -> Mapped[int | None]:
    return mapped_column(BigInteger, ForeignKey("users.id"), nullable=nullable)


# ---------------------------------------------------------------- оргструктура и доступ


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[dt.datetime] = _now_col()


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[dt.datetime] = _now_col()


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    position: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[dt.datetime] = _now_col()
    dismissed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class EmployeeDepartment(Base):
    __tablename__ = "employee_departments"
    __table_args__ = (
        UniqueConstraint("employee_id", "department_id"),
        Index("ix_employee_departments_dept", "department_id", postgresql_where=text("is_active")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[dt.datetime] = _now_col()


class Role(Base):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[dt.datetime] = _now_col()


class AccessGrant(Base):
    """Выдача роли пользователю. department_id IS NULL — роль действует во всех подразделениях."""

    __tablename__ = "access_grants"
    __table_args__ = (Index("ix_access_grants_active_user", "user_id", postgresql_where=text("revoked_at IS NULL")),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    role_code: Mapped[str] = mapped_column(ForeignKey("roles.code"), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    granted_by: Mapped[int | None] = _user_fk()
    granted_at: Mapped[dt.datetime] = _now_col()
    revoked_by: Mapped[int | None] = _user_fk()
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramChat(Base):
    """Рабочая группа, привязанная к подразделению."""

    __tablename__ = "telegram_chats"
    __table_args__ = (Index("ix_telegram_chats_dept", "department_id", postgresql_where=text("is_active")),)

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    bound_by: Mapped[int | None] = _user_fk()
    bound_at: Mapped[dt.datetime] = _now_col()


# ---------------------------------------------------------------- справочники и настройки


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[int | None] = _user_fk()
    updated_at: Mapped[dt.datetime] = _now_col()


class Currency(Base):
    __tablename__ = "currencies"
    __table_args__ = (CheckConstraint("minor_units BETWEEN 0 AND 4", name="minor_units"),)

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    minor_units: Mapped[int] = mapped_column(Integer, nullable=False)


class Unit(Base):
    """Единица измерения. scale — сколько знаков после запятой допустимо (шт = 0, кг = 3)."""

    __tablename__ = "units"
    __table_args__ = (CheckConstraint("scale BETWEEN 0 AND 6", name="scale"),)

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    scale: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------------------------------------------------------------- работы и тарифы


class WorkType(Base):
    __tablename__ = "work_types"
    __table_args__ = (CheckConstraint("calc_method IN ('per_unit', 'fixed', 'percent')", name="calc_method"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    unit_code: Mapped[str] = mapped_column(ForeignKey("units.code"), nullable=False)
    calc_method: Mapped[str] = mapped_column(String(16), nullable=False, default="per_unit")
    # Переопределение округления для вида работ (например, до целых). NULL — по валюте и настройкам.
    rounding_step: Mapped[Decimal | None] = mapped_column(MONEY)
    rounding_mode: Mapped[str | None] = mapped_column(String(16))
    all_departments: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_by: Mapped[int | None] = _user_fk()
    created_at: Mapped[dt.datetime] = _now_col()


class WorkTypeDepartment(Base):
    __tablename__ = "work_type_departments"

    work_type_id: Mapped[int] = mapped_column(ForeignKey("work_types.id"), primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), primary_key=True)


class Tariff(Base):
    """Ставка. Строка не изменяется: новая ставка — новая строка с датой начала действия.

    Область действия: department_id / employee_id могут быть NULL (общая ставка).
    Приоритет: сотрудник+подразделение > сотрудник > подразделение > общая.
    """

    __tablename__ = "tariffs"
    __table_args__ = (
        CheckConstraint("rate >= 0", name="rate_non_negative"),
        Index("ix_tariffs_lookup", "work_type_id", "employee_id", "department_id", "valid_from"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_type_id: Mapped[int] = mapped_column(ForeignKey("work_types.id"), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    valid_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = _user_fk()
    created_at: Mapped[dt.datetime] = _now_col()


class TariffTier(Base):
    """Ступень шкалы: единицы сверх threshold (накопленно за расчётный период) оплачиваются по rate.

    Первая ступень (от 0) — это Tariff.rate. Строки не изменяются, как и сам тариф.
    """

    __tablename__ = "tariff_tiers"
    __table_args__ = (
        UniqueConstraint("tariff_id", "threshold"),
        CheckConstraint("threshold > 0", name="threshold_positive"),
        CheckConstraint("rate >= 0", name="rate_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"), nullable=False)
    threshold: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)


class Coefficient(Base):
    """Повышающий/понижающий коэффициент к операции: «срочно» ×1,5, «выходной» ×2."""

    __tablename__ = "coefficients"
    __table_args__ = (CheckConstraint("factor > 0", name="factor_positive"),)

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    factor: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_by: Mapped[int | None] = _user_fk()
    created_at: Mapped[dt.datetime] = _now_col()


class WorkEntry(Base):
    """Выполненная операция. Ставка и сумма копируются в строку и после подтверждения не меняются."""

    __tablename__ = "work_entries"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("status IN ('draft', 'pending', 'confirmed', 'rejected', 'cancelled')", name="status"),
        CheckConstraint("coefficient > 0", name="coefficient_positive"),
        Index("ix_work_entries_dept_date", "department_id", "performed_on"),
        Index("ix_work_entries_emp_date", "employee_id", "performed_on"),
        # Очередь на подтверждение и пересчёт после тарифа задним числом — только открытые записи.
        Index(
            "ix_work_entries_open",
            "department_id",
            "work_type_id",
            "performed_on",
            postgresql_where=text("status IN ('draft', 'pending')"),
        ),
        # Накопленный объём для ступенчатых тарифов.
        Index(
            "ix_work_entries_confirmed_volume",
            "employee_id",
            "work_type_id",
            "department_id",
            "performed_on",
            postgresql_where=text("status = 'confirmed'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    work_type_id: Mapped[int] = mapped_column(ForeignKey("work_types.id"), nullable=False)
    order_ref: Mapped[str | None] = mapped_column(String(64))
    performed_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    unit_code: Mapped[str] = mapped_column(ForeignKey("units.code"), nullable=False)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"), nullable=False)
    rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    coefficient: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal(1), server_default=text("1")
    )
    coefficient_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String(32)))
    # Расшифровка расчёта: куски по ступеням/ставкам, метод, коэффициенты.
    calc_details: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[dt.datetime] = _now_col()
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[int | None] = _user_fk()
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column(Text)
    cancelled_by: Mapped[int | None] = _user_fk()
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)

    @property
    def number(self) -> str:
        return f"ОП-{self.id:06d}"


# ---------------------------------------------------------------- взаиморасчёты


class PayrollPeriod(Base):
    __tablename__ = "payroll_periods"
    __table_args__ = (
        CheckConstraint("date_from <= date_to", name="dates"),
        CheckConstraint("status IN ('open', 'closed')", name="status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date_from: Mapped[dt.date] = mapped_column(Date, unique=True, nullable=False)
    date_to: Mapped[dt.date] = mapped_column(Date, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    closed_by: Mapped[int | None] = _user_fk()
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def title(self) -> str:
        return f"{self.date_from:%d.%m.%Y}–{self.date_to:%d.%m.%Y}"


class PeriodReopening(Base):
    __tablename__ = "period_reopenings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("payroll_periods.id"), nullable=False)
    reopened_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    reopened_at: Mapped[dt.datetime] = _now_col()
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class PeriodStatement(Base):
    """Итоги сотрудника за период, зафиксированные при закрытии. Каждое закрытие — новая версия."""

    __tablename__ = "period_statements"
    __table_args__ = (UniqueConstraint("period_id", "version", "employee_id", "currency"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("payroll_periods.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    opening: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    accrued: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    deducted: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    corrections: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    paid: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    closing: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    entries_count: Mapped[int] = mapped_column(Integer, nullable=False)
    entries_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = _now_col()


LEDGER_TYPES = ("work", "bonus", "penalty", "deduction", "payout", "reversal")


class LedgerEntry(Base):
    """Журнал взаиморасчётов с сотрудником. amount > 0 — компания должна сотруднику, < 0 — наоборот.

    Уникальность (source_type, source_id, entry_type) не даёт начислить дважды за один документ.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "entry_type"),
        CheckConstraint("amount <> 0", name="amount_non_zero"),
        CheckConstraint(
            "entry_type IN ('work', 'bonus', 'penalty', 'deduction', 'payout', 'reversal')", name="entry_type"
        ),
        Index("ix_ledger_employee", "employee_id", "currency"),
        Index("ix_ledger_employee_period", "employee_id", "period_id"),
        Index("ix_ledger_employee_date", "employee_id", "business_date"),
        Index("ix_ledger_period", "period_id"),
        Index("ix_ledger_dept_date", "department_id", "business_date"),
        Index("ix_ledger_business_date", "business_date"),
        Index("ix_ledger_reverses", "reverses_id", postgresql_where=text("reverses_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reverses_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("ledger_entries.id"))
    period_id: Mapped[int] = mapped_column(ForeignKey("payroll_periods.id"), nullable=False)
    business_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    posted_at: Mapped[dt.datetime] = _now_col()
    posted_by: Mapped[int | None] = _user_fk()
    comment: Mapped[str | None] = mapped_column(Text)


class Adjustment(Base):
    """Премия, штраф или удержание. В журнал попадает только после подписи (и согласия, если включено)."""

    __tablename__ = "adjustments"
    __table_args__ = (
        CheckConstraint("kind IN ('bonus', 'penalty', 'deduction')", name="kind"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "status IN ('pending', 'awaiting_consent', 'approved', 'rejected', 'disputed', 'cancelled')",
            name="status",
        ),
        CheckConstraint("withheld >= 0 AND withheld <= amount", name="withheld_range"),
        Index("ix_adjustments_employee", "employee_id", "business_date"),
        Index("ix_adjustments_date", "business_date"),
        Index(
            "ix_adjustments_open",
            "department_id",
            postgresql_where=text("status IN ('pending', 'awaiting_consent', 'disputed')"),
        ),
        # Удержания, которые ещё не удержаны полностью (лимит удержаний).
        Index(
            "ix_adjustments_outstanding",
            "employee_id",
            "currency",
            postgresql_where=text("status = 'approved' AND kind <> 'bonus' AND withheld < amount"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    business_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    basis_type: Mapped[str | None] = mapped_column(String(32))
    basis_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[dt.datetime] = _now_col()
    decided_by: Mapped[int | None] = _user_fk()
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column(Text)
    consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Сколько уже удержано (проведено в журнал). При лимите удержаний сумма удерживается частями.
    withheld: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0), server_default=text("0"))

    @property
    def number(self) -> str:
        prefix = {"bonus": "ПР", "penalty": "ШТ", "deduction": "УД"}[self.kind]
        return f"{prefix}-{self.id:05d}"

    @property
    def outstanding(self) -> Decimal:
        return self.amount - self.withheld


class AdjustmentInstallment(Base):
    """Часть удержания, проведённая в журнал в рамках лимита удержаний."""

    __tablename__ = "adjustment_installments"
    __table_args__ = (
        UniqueConstraint("adjustment_id", "seq"),
        CheckConstraint("amount > 0", name="amount_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    adjustment_id: Mapped[int] = mapped_column(ForeignKey("adjustments.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("payroll_periods.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_at: Mapped[dt.datetime] = _now_col()


class EmployeeCreditLimit(Base):
    """Сколько можно выдать сотруднику сверх заработанного (аванс в долг)."""

    __tablename__ = "employee_credit_limits"
    __table_args__ = (CheckConstraint("amount >= 0", name="amount_non_negative"),)

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), primary_key=True)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    set_by: Mapped[int | None] = _user_fk()
    set_at: Mapped[dt.datetime] = _now_col()


class Payout(Base):
    __tablename__ = "payouts"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("kind IN ('advance', 'salary', 'on_demand')", name="kind"),
        CheckConstraint("status IN ('registered', 'confirmed', 'cancelled')", name="status"),
        Index("ix_payouts_employee", "employee_id", "business_date"),
        Index("ix_payouts_date", "business_date"),
        Index("ix_payouts_registered", "employee_id", postgresql_where=text("status = 'registered'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    method: Mapped[str | None] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    business_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    request_id: Mapped[int | None] = mapped_column(BigInteger)
    registered_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    registered_at: Mapped[dt.datetime] = _now_col()
    confirmed_by: Mapped[int | None] = _user_fk()
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[int | None] = _user_fk()
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    @property
    def number(self) -> str:
        return f"В-{self.id:05d}"


class PayoutRequest(Base):
    __tablename__ = "payout_requests"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("status IN ('requested', 'approved', 'rejected', 'cancelled')", name="status"),
        Index("ix_payout_requests_open", "employee_id", postgresql_where=text("status = 'requested'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="requested")
    created_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[dt.datetime] = _now_col()
    decided_by: Mapped[int | None] = _user_fk()
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column(Text)
    payout_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("payouts.id"))

    @property
    def number(self) -> str:
        return f"ЗВ-{self.id:05d}"


# ---------------------------------------------------------------- склад


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128))
    base_unit: Mapped[str] = mapped_column(ForeignKey("units.code"), nullable=False)
    track_batches: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[dt.datetime] = _now_col()


class ProductUnit(Base):
    """Альтернативная единица товара: 1 unit_code = factor базовых единиц."""

    __tablename__ = "product_units"
    __table_args__ = (CheckConstraint("factor > 0", name="factor_positive"),)

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    unit_code: Mapped[str] = mapped_column(ForeignKey("units.code"), primary_key=True)
    factor: Mapped[Decimal] = mapped_column(QTY, nullable=False)


class Batch(Base):
    """Партия: создаётся при каждом поступлении. Списание идёт по FIFO, себестоимость — за базовую единицу."""

    __tablename__ = "batches"
    __table_args__ = (
        Index("ix_batches_fifo", "product_id", "received_on", "id"),
        CheckConstraint("unit_cost IS NULL OR unit_cost >= 0", name="unit_cost_non_negative"),
        CheckConstraint("(unit_cost IS NULL) = (currency IS NULL)", name="cost_has_currency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    document_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("stock_documents.id"))
    received_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    qty_received: Mapped[Decimal | None] = mapped_column(QTY)
    unit_cost: Mapped[Decimal | None] = mapped_column(RATE)
    currency: Mapped[str | None] = mapped_column(ForeignKey("currencies.code"))
    created_at: Mapped[dt.datetime] = _now_col()

    @property
    def number(self) -> str:
        return f"ПТ-{self.id:05d}"


LOCATION_KINDS = ("warehouse", "employee", "transit", "supplier", "customer", "writeoff", "shortage", "surplus")
VIRTUAL_LOCATION_KINDS = frozenset({"supplier", "customer", "writeoff", "shortage", "surplus"})


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (
        UniqueConstraint("kind", "department_id", "employee_id", postgresql_nulls_not_distinct=True),
        CheckConstraint(
            "kind IN ('warehouse', 'employee', 'transit', 'supplier', 'customer', 'writeoff', 'shortage', "
            "'surplus')",
            name="kind",
        ),
        Index("ix_locations_employee", "employee_id", postgresql_where=text("employee_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))


STOCK_DOC_TYPES = ("receipt", "issue", "return", "move", "send", "delivery", "writeoff", "inventory")


class StockDocument(Base):
    __tablename__ = "stock_documents"
    __table_args__ = (
        CheckConstraint(
            "doc_type IN ('receipt', 'issue', 'return', 'move', 'send', 'delivery', 'writeoff', 'inventory')",
            name="doc_type",
        ),
        CheckConstraint("status IN ('posted', 'in_transit', 'completed', 'returned')", name="status"),
        Index("ix_stock_documents_dept", "department_id", "created_at"),
        Index("ix_stock_documents_in_transit", "id", postgresql_where=text("status = 'in_transit'")),
        Index(
            "ix_stock_documents_target_dept",
            "target_department_id",
            postgresql_where=text("target_department_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    doc_type: Mapped[str] = mapped_column(String(16), nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    target_department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    target_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    order_ref: Mapped[str | None] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[dt.datetime] = _now_col()
    accepted_by: Mapped[int | None] = _user_fk()
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)

    @property
    def number(self) -> str:
        prefix = {
            "receipt": "П",
            "issue": "ВД",
            "return": "ВЗ",
            "move": "ПМ",
            "send": "ПР",
            "delivery": "ДС",
            "writeoff": "СП",
            "inventory": "ИН",
        }[self.doc_type]
        return f"{prefix}-{self.id:05d}"


class StockDocumentLine(Base):
    __tablename__ = "stock_document_lines"
    __table_args__ = (
        CheckConstraint("qty_base > 0", name="qty_positive"),
        Index("ix_stock_document_lines_document", "document_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("stock_documents.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty_entered: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    unit_entered: Mapped[str] = mapped_column(ForeignKey("units.code"), nullable=False)
    qty_base: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    from_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    to_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    # Для перемещений «в пути»: куда товар попадёт после подтверждения получения.
    final_location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))


class StockMove(Base):
    __tablename__ = "stock_moves"
    __table_args__ = (
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("from_location_id <> to_location_id", name="different_locations"),
        Index("ix_stock_moves_product", "product_id", "posted_at"),
        Index("ix_stock_moves_document", "document_id"),
        Index("ix_stock_moves_posted_at", "posted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("stock_documents.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    from_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    to_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    posted_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    posted_at: Mapped[dt.datetime] = _now_col()


class StockBalance(Base):
    """Текущий остаток. Пересчитывается в той же транзакции, что и движение; сверяется с stock_moves."""

    __tablename__ = "stock_balances"
    __table_args__ = (
        CheckConstraint("allow_negative OR qty >= 0", name="non_negative"),
        Index(
            "uq_stock_balances_no_batch",
            "location_id",
            "product_id",
            unique=True,
            postgresql_where=text("batch_id IS NULL"),
        ),
        Index(
            "uq_stock_balances_batch",
            "location_id",
            "product_id",
            "batch_id",
            unique=True,
            postgresql_where=text("batch_id IS NOT NULL"),
        ),
        Index("ix_stock_balances_product", "product_id"),
        # Остаток места по товару по всем партиям (FIFO, проверка достаточности).
        Index("ix_stock_balances_location_product", "location_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    allow_negative: Mapped[bool] = mapped_column(Boolean, nullable=False)


class DiscrepancyCase(Base):
    """Расхождение по инвентаризации. Денежные последствия — только через решение и Adjustment."""

    __tablename__ = "discrepancy_cases"
    __table_args__ = (
        CheckConstraint("kind IN ('shortage', 'surplus')", name="kind"),
        CheckConstraint("status IN ('open', 'resolved')", name="status"),
        CheckConstraint("decision IS NULL OR decision IN ('company', 'employee')", name="decision"),
        Index("ix_discrepancy_cases_open", "department_id", postgresql_where=text("status = 'open'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("stock_documents.id"), nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    expected_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    counted_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    decision: Mapped[str | None] = mapped_column(String(16))
    price: Mapped[Decimal | None] = mapped_column(RATE)
    currency: Mapped[str | None] = mapped_column(ForeignKey("currencies.code"))
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    price_basis: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[int | None] = _user_fk()
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    adjustment_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("adjustments.id"))
    created_at: Mapped[dt.datetime] = _now_col()

    @property
    def number(self) -> str:
        return f"Р-{self.id:05d}"


# ---------------------------------------------------------------- аудит


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_actor", "actor_user_id", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[dt.datetime] = _now_col()
    actor_user_id: Mapped[int | None] = _user_fk()
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict | None] = mapped_column(JSONB)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)


# ---------------------------------------------------------------- состояние диалогов


class FsmState(Base):
    """Состояние мастеров ввода aiogram. В БД, чтобы перезапуск бота не сбрасывал незаконченный ввод."""

    __tablename__ = "fsm_states"
    __table_args__ = (Index("ix_fsm_states_updated", "updated_at"),)

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(200))
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
