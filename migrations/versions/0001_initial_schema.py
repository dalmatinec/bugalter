"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-11 00:45:28.433860
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "currencies",
        sa.Column("code", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("minor_units", sa.Integer(), nullable=False),
        sa.CheckConstraint("minor_units BETWEEN 0 AND 4", name=op.f("ck_currencies_minor_units")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_currencies")),
    )
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_departments")),
        sa.UniqueConstraint("code", name=op.f("uq_departments_code")),
    )
    op.create_table(
        "roles",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("permissions", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_roles")),
    )
    op.create_table(
        "units",
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("scale", sa.Integer(), nullable=False),
        sa.CheckConstraint("scale BETWEEN 0 AND 6", name=op.f("ck_units_scale")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_units")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("full_name", sa.String(length=256), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_users_telegram_id")),
    )
    op.create_table(
        "access_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role_code", sa.String(length=32), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("granted_by", sa.BigInteger(), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_access_grants_department_id_departments")
        ),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], name=op.f("fk_access_grants_granted_by_users")),
        sa.ForeignKeyConstraint(["revoked_by"], ["users.id"], name=op.f("fk_access_grants_revoked_by_users")),
        sa.ForeignKeyConstraint(["role_code"], ["roles.code"], name=op.f("fk_access_grants_role_code_roles")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_access_grants_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_grants")),
    )
    op.create_index(
        "ix_access_grants_active_user",
        "access_grants",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], name=op.f("fk_audit_log_actor_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_entity", "audit_log", ["entity_type", "entity_id"], unique=False)
    op.create_table(
        "employees",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("full_name", sa.String(length=256), nullable=False),
        sa.Column("position", sa.String(length=128), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_employees_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employees")),
        sa.UniqueConstraint("code", name=op.f("uq_employees_code")),
        sa.UniqueConstraint("user_id", name=op.f("uq_employees_user_id")),
    )
    op.create_table(
        "payroll_periods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_payroll_periods_status")),
        sa.CheckConstraint("date_from <= date_to", name=op.f("ck_payroll_periods_dates")),
        sa.ForeignKeyConstraint(["closed_by"], ["users.id"], name=op.f("fk_payroll_periods_closed_by_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payroll_periods")),
        sa.UniqueConstraint("date_from", name=op.f("uq_payroll_periods_date_from")),
        sa.UniqueConstraint("date_to", name=op.f("uq_payroll_periods_date_to")),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("base_unit", sa.String(length=16), nullable=False),
        sa.Column("track_batches", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["base_unit"], ["units.code"], name=op.f("fk_products_base_unit_units")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("sku", name=op.f("uq_products_sku")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name=op.f("fk_settings_updated_by_users")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "telegram_chats",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("bound_by", sa.BigInteger(), nullable=True),
        sa.Column("bound_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["bound_by"], ["users.id"], name=op.f("fk_telegram_chats_bound_by_users")),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_telegram_chats_department_id_departments")
        ),
        sa.PrimaryKeyConstraint("chat_id", name=op.f("pk_telegram_chats")),
    )
    op.create_table(
        "work_types",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("unit_code", sa.String(length=16), nullable=False),
        sa.Column("calc_method", sa.String(length=16), nullable=False),
        sa.Column("rounding_step", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("rounding_mode", sa.String(length=16), nullable=True),
        sa.Column("all_departments", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("calc_method IN ('per_unit', 'fixed')", name=op.f("ck_work_types_calc_method")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_work_types_created_by_users")),
        sa.ForeignKeyConstraint(["unit_code"], ["units.code"], name=op.f("fk_work_types_unit_code_units")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_types")),
        sa.UniqueConstraint("code", name=op.f("uq_work_types_code")),
    )
    op.create_table(
        "adjustments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("basis_type", sa.String(length=32), nullable=True),
        sa.Column("basis_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('bonus', 'penalty', 'deduction')", name=op.f("ck_adjustments_kind")),
        sa.CheckConstraint(
            "status IN ('pending', 'awaiting_consent', 'approved', 'rejected', 'disputed', 'cancelled')",
            name=op.f("ck_adjustments_status"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_adjustments_amount_positive")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_adjustments_created_by_users")),
        sa.ForeignKeyConstraint(["currency"], ["currencies.code"], name=op.f("fk_adjustments_currency_currencies")),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], name=op.f("fk_adjustments_decided_by_users")),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_adjustments_department_id_departments")
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], name=op.f("fk_adjustments_employee_id_employees")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_adjustments")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_adjustments_idempotency_key")),
    )
    op.create_table(
        "batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["currency"], ["currencies.code"], name=op.f("fk_batches_currency_currencies")),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_batches_product_id_products")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_batches")),
    )
    op.create_table(
        "employee_departments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_employee_departments_department_id_departments")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_employee_departments_employee_id_employees")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_departments")),
        sa.UniqueConstraint(
            "employee_id", "department_id", name=op.f("uq_employee_departments_employee_id_department_id")
        ),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("entry_type", sa.String(length=16), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("reverses_id", sa.BigInteger(), nullable=True),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("posted_by", sa.BigInteger(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "entry_type IN ('work', 'bonus', 'penalty', 'deduction', 'payout', 'reversal')",
            name=op.f("ck_ledger_entries_entry_type"),
        ),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_ledger_entries_amount_non_zero")),
        sa.ForeignKeyConstraint(["currency"], ["currencies.code"], name=op.f("fk_ledger_entries_currency_currencies")),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_ledger_entries_department_id_departments")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_ledger_entries_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(
            ["period_id"], ["payroll_periods.id"], name=op.f("fk_ledger_entries_period_id_payroll_periods")
        ),
        sa.ForeignKeyConstraint(["posted_by"], ["users.id"], name=op.f("fk_ledger_entries_posted_by_users")),
        sa.ForeignKeyConstraint(
            ["reverses_id"], ["ledger_entries.id"], name=op.f("fk_ledger_entries_reverses_id_ledger_entries")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_entries")),
        sa.UniqueConstraint(
            "source_type", "source_id", "entry_type", name=op.f("uq_ledger_entries_source_type_source_id_entry_type")
        ),
    )
    op.create_index("ix_ledger_business_date", "ledger_entries", ["business_date"], unique=False)
    op.create_index("ix_ledger_employee", "ledger_entries", ["employee_id", "currency"], unique=False)
    op.create_index("ix_ledger_period", "ledger_entries", ["period_id"], unique=False)
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('warehouse', 'employee', 'transit', 'supplier', 'customer', 'writeoff', 'shortage', 'surplus')",
            name=op.f("ck_locations_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_locations_department_id_departments")
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], name=op.f("fk_locations_employee_id_employees")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_locations")),
        sa.UniqueConstraint(
            "kind",
            "department_id",
            "employee_id",
            name=op.f("uq_locations_kind_department_id_employee_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "payouts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("method", sa.String(length=64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
        sa.Column("registered_by", sa.BigInteger(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("confirmed_by", sa.BigInteger(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.BigInteger(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('advance', 'salary', 'on_demand')", name=op.f("ck_payouts_kind")),
        sa.CheckConstraint("status IN ('registered', 'confirmed', 'cancelled')", name=op.f("ck_payouts_status")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payouts_amount_positive")),
        sa.ForeignKeyConstraint(["cancelled_by"], ["users.id"], name=op.f("fk_payouts_cancelled_by_users")),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], name=op.f("fk_payouts_confirmed_by_users")),
        sa.ForeignKeyConstraint(["currency"], ["currencies.code"], name=op.f("fk_payouts_currency_currencies")),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_payouts_department_id_departments")
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], name=op.f("fk_payouts_employee_id_employees")),
        sa.ForeignKeyConstraint(["registered_by"], ["users.id"], name=op.f("fk_payouts_registered_by_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payouts")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_payouts_idempotency_key")),
    )
    op.create_table(
        "period_reopenings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("reopened_by", sa.BigInteger(), nullable=False),
        sa.Column("reopened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["period_id"], ["payroll_periods.id"], name=op.f("fk_period_reopenings_period_id_payroll_periods")
        ),
        sa.ForeignKeyConstraint(["reopened_by"], ["users.id"], name=op.f("fk_period_reopenings_reopened_by_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_period_reopenings")),
    )
    op.create_table(
        "period_statements",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("opening", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("accrued", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("deducted", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("corrections", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("paid", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("closing", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("entries_count", sa.Integer(), nullable=False),
        sa.Column("entries_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["currency"], ["currencies.code"], name=op.f("fk_period_statements_currency_currencies")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_period_statements_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(
            ["period_id"], ["payroll_periods.id"], name=op.f("fk_period_statements_period_id_payroll_periods")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_period_statements")),
        sa.UniqueConstraint(
            "period_id",
            "version",
            "employee_id",
            "currency",
            name=op.f("uq_period_statements_period_id_version_employee_id_currency"),
        ),
    )
    op.create_table(
        "product_units",
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("unit_code", sa.String(length=16), nullable=False),
        sa.Column("factor", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.CheckConstraint("factor > 0", name=op.f("ck_product_units_factor_positive")),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_product_units_product_id_products")),
        sa.ForeignKeyConstraint(["unit_code"], ["units.code"], name=op.f("fk_product_units_unit_code_units")),
        sa.PrimaryKeyConstraint("product_id", "unit_code", name=op.f("pk_product_units")),
    )
    op.create_table(
        "stock_documents",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("doc_type", sa.String(length=16), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("target_department_id", sa.Integer(), nullable=True),
        sa.Column("employee_id", sa.Integer(), nullable=True),
        sa.Column("target_employee_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("order_ref", sa.String(length=64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("accepted_by", sa.BigInteger(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "doc_type IN ('receipt', 'issue', 'return', 'move', 'send', 'delivery', 'writeoff', 'inventory')",
            name=op.f("ck_stock_documents_doc_type"),
        ),
        sa.CheckConstraint(
            "status IN ('posted', 'in_transit', 'completed', 'returned')", name=op.f("ck_stock_documents_status")
        ),
        sa.ForeignKeyConstraint(["accepted_by"], ["users.id"], name=op.f("fk_stock_documents_accepted_by_users")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_stock_documents_created_by_users")),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_stock_documents_department_id_departments")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_stock_documents_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(
            ["target_department_id"],
            ["departments.id"],
            name=op.f("fk_stock_documents_target_department_id_departments"),
        ),
        sa.ForeignKeyConstraint(
            ["target_employee_id"], ["employees.id"], name=op.f("fk_stock_documents_target_employee_id_employees")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_documents")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_stock_documents_idempotency_key")),
    )
    op.create_index("ix_stock_documents_dept", "stock_documents", ["department_id", "created_at"], unique=False)
    op.create_table(
        "tariffs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_type_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("employee_id", sa.Integer(), nullable=True),
        sa.Column("rate", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("rate >= 0", name=op.f("ck_tariffs_rate_non_negative")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_tariffs_created_by_users")),
        sa.ForeignKeyConstraint(["currency"], ["currencies.code"], name=op.f("fk_tariffs_currency_currencies")),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_tariffs_department_id_departments")
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], name=op.f("fk_tariffs_employee_id_employees")),
        sa.ForeignKeyConstraint(["work_type_id"], ["work_types.id"], name=op.f("fk_tariffs_work_type_id_work_types")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tariffs")),
    )
    op.create_index("ix_tariffs_lookup", "tariffs", ["work_type_id", "valid_from"], unique=False)
    op.create_table(
        "work_type_departments",
        sa.Column("work_type_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_work_type_departments_department_id_departments")
        ),
        sa.ForeignKeyConstraint(
            ["work_type_id"], ["work_types.id"], name=op.f("fk_work_type_departments_work_type_id_work_types")
        ),
        sa.PrimaryKeyConstraint("work_type_id", "department_id", name=op.f("pk_work_type_departments")),
    )
    op.create_table(
        "discrepancy_cases",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("expected_qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("counted_qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("price", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("price_basis", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("adjustment_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('company', 'employee')", name=op.f("ck_discrepancy_cases_decision")
        ),
        sa.CheckConstraint("kind IN ('shortage', 'surplus')", name=op.f("ck_discrepancy_cases_kind")),
        sa.CheckConstraint("status IN ('open', 'resolved')", name=op.f("ck_discrepancy_cases_status")),
        sa.ForeignKeyConstraint(
            ["adjustment_id"], ["adjustments.id"], name=op.f("fk_discrepancy_cases_adjustment_id_adjustments")
        ),
        sa.ForeignKeyConstraint(
            ["currency"], ["currencies.code"], name=op.f("fk_discrepancy_cases_currency_currencies")
        ),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], name=op.f("fk_discrepancy_cases_decided_by_users")),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_discrepancy_cases_department_id_departments")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["stock_documents.id"], name=op.f("fk_discrepancy_cases_document_id_stock_documents")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_discrepancy_cases_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(
            ["location_id"], ["locations.id"], name=op.f("fk_discrepancy_cases_location_id_locations")
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_discrepancy_cases_product_id_products")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discrepancy_cases")),
    )
    op.create_table(
        "payout_requests",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("payout_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "status IN ('requested', 'approved', 'rejected', 'cancelled')", name=op.f("ck_payout_requests_status")
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payout_requests_amount_positive")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_payout_requests_created_by_users")),
        sa.ForeignKeyConstraint(["currency"], ["currencies.code"], name=op.f("fk_payout_requests_currency_currencies")),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], name=op.f("fk_payout_requests_decided_by_users")),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_payout_requests_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(["payout_id"], ["payouts.id"], name=op.f("fk_payout_requests_payout_id_payouts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payout_requests")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_payout_requests_idempotency_key")),
    )
    op.create_table(
        "stock_balances",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=True),
        sa.Column("qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("allow_negative", sa.Boolean(), nullable=False),
        sa.CheckConstraint("allow_negative OR qty >= 0", name=op.f("ck_stock_balances_non_negative")),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"], name=op.f("fk_stock_balances_batch_id_batches")),
        sa.ForeignKeyConstraint(
            ["location_id"], ["locations.id"], name=op.f("fk_stock_balances_location_id_locations")
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_stock_balances_product_id_products")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_balances")),
    )
    op.create_index(
        "uq_stock_balances_batch",
        "stock_balances",
        ["location_id", "product_id", "batch_id"],
        unique=True,
        postgresql_where=sa.text("batch_id IS NOT NULL"),
    )
    op.create_index(
        "uq_stock_balances_no_batch",
        "stock_balances",
        ["location_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("batch_id IS NULL"),
    )
    op.create_table(
        "stock_document_lines",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("qty_entered", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("unit_entered", sa.String(length=16), nullable=False),
        sa.Column("qty_base", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("from_location_id", sa.Integer(), nullable=False),
        sa.Column("to_location_id", sa.Integer(), nullable=False),
        sa.Column("final_location_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("qty_base > 0", name=op.f("ck_stock_document_lines_qty_positive")),
        sa.ForeignKeyConstraint(
            ["document_id"], ["stock_documents.id"], name=op.f("fk_stock_document_lines_document_id_stock_documents")
        ),
        sa.ForeignKeyConstraint(
            ["final_location_id"], ["locations.id"], name=op.f("fk_stock_document_lines_final_location_id_locations")
        ),
        sa.ForeignKeyConstraint(
            ["from_location_id"], ["locations.id"], name=op.f("fk_stock_document_lines_from_location_id_locations")
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name=op.f("fk_stock_document_lines_product_id_products")
        ),
        sa.ForeignKeyConstraint(
            ["to_location_id"], ["locations.id"], name=op.f("fk_stock_document_lines_to_location_id_locations")
        ),
        sa.ForeignKeyConstraint(
            ["unit_entered"], ["units.code"], name=op.f("fk_stock_document_lines_unit_entered_units")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_document_lines")),
    )
    op.create_table(
        "stock_moves",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=True),
        sa.Column("from_location_id", sa.Integer(), nullable=False),
        sa.Column("to_location_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("posted_by", sa.BigInteger(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("from_location_id <> to_location_id", name=op.f("ck_stock_moves_different_locations")),
        sa.CheckConstraint("qty > 0", name=op.f("ck_stock_moves_qty_positive")),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"], name=op.f("fk_stock_moves_batch_id_batches")),
        sa.ForeignKeyConstraint(
            ["document_id"], ["stock_documents.id"], name=op.f("fk_stock_moves_document_id_stock_documents")
        ),
        sa.ForeignKeyConstraint(
            ["from_location_id"], ["locations.id"], name=op.f("fk_stock_moves_from_location_id_locations")
        ),
        sa.ForeignKeyConstraint(["posted_by"], ["users.id"], name=op.f("fk_stock_moves_posted_by_users")),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_stock_moves_product_id_products")),
        sa.ForeignKeyConstraint(
            ["to_location_id"], ["locations.id"], name=op.f("fk_stock_moves_to_location_id_locations")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_moves")),
    )
    op.create_index("ix_stock_moves_product", "stock_moves", ["product_id", "posted_at"], unique=False)
    op.create_table(
        "work_entries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("work_type_id", sa.Integer(), nullable=False),
        sa.Column("order_ref", sa.String(length=64), nullable=True),
        sa.Column("performed_on", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("unit_code", sa.String(length=16), nullable=False),
        sa.Column("tariff_id", sa.Integer(), nullable=False),
        sa.Column("rate", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("cancelled_by", sa.BigInteger(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'pending', 'confirmed', 'rejected', 'cancelled')", name=op.f("ck_work_entries_status")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_work_entries_quantity_positive")),
        sa.ForeignKeyConstraint(["cancelled_by"], ["users.id"], name=op.f("fk_work_entries_cancelled_by_users")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_work_entries_created_by_users")),
        sa.ForeignKeyConstraint(["currency"], ["currencies.code"], name=op.f("fk_work_entries_currency_currencies")),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], name=op.f("fk_work_entries_decided_by_users")),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], name=op.f("fk_work_entries_department_id_departments")
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], name=op.f("fk_work_entries_employee_id_employees")),
        sa.ForeignKeyConstraint(["tariff_id"], ["tariffs.id"], name=op.f("fk_work_entries_tariff_id_tariffs")),
        sa.ForeignKeyConstraint(["unit_code"], ["units.code"], name=op.f("fk_work_entries_unit_code_units")),
        sa.ForeignKeyConstraint(
            ["work_type_id"], ["work_types.id"], name=op.f("fk_work_entries_work_type_id_work_types")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_entries")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_work_entries_idempotency_key")),
    )
    op.create_index("ix_work_entries_dept_date", "work_entries", ["department_id", "performed_on"], unique=False)
    op.create_index("ix_work_entries_emp_date", "work_entries", ["employee_id", "performed_on"], unique=False)
    op.create_index("ix_work_entries_status", "work_entries", ["status"], unique=False)

    install_guards()


def downgrade() -> None:
    drop_guards()
    op.drop_index("ix_work_entries_status", table_name="work_entries")
    op.drop_index("ix_work_entries_emp_date", table_name="work_entries")
    op.drop_index("ix_work_entries_dept_date", table_name="work_entries")
    op.drop_table("work_entries")
    op.drop_index("ix_stock_moves_product", table_name="stock_moves")
    op.drop_table("stock_moves")
    op.drop_table("stock_document_lines")
    op.drop_index(
        "uq_stock_balances_no_batch", table_name="stock_balances", postgresql_where=sa.text("batch_id IS NULL")
    )
    op.drop_index(
        "uq_stock_balances_batch", table_name="stock_balances", postgresql_where=sa.text("batch_id IS NOT NULL")
    )
    op.drop_table("stock_balances")
    op.drop_table("payout_requests")
    op.drop_table("discrepancy_cases")
    op.drop_table("work_type_departments")
    op.drop_index("ix_tariffs_lookup", table_name="tariffs")
    op.drop_table("tariffs")
    op.drop_index("ix_stock_documents_dept", table_name="stock_documents")
    op.drop_table("stock_documents")
    op.drop_table("product_units")
    op.drop_table("period_statements")
    op.drop_table("period_reopenings")
    op.drop_table("payouts")
    op.drop_table("locations")
    op.drop_index("ix_ledger_period", table_name="ledger_entries")
    op.drop_index("ix_ledger_employee", table_name="ledger_entries")
    op.drop_index("ix_ledger_business_date", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_table("employee_departments")
    op.drop_table("batches")
    op.drop_table("adjustments")
    op.drop_table("work_types")
    op.drop_table("telegram_chats")
    op.drop_table("settings")
    op.drop_table("products")
    op.drop_table("payroll_periods")
    op.drop_table("employees")
    op.drop_index("ix_audit_entity", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index(
        "ix_access_grants_active_user", table_name="access_grants", postgresql_where=sa.text("revoked_at IS NULL")
    )
    op.drop_table("access_grants")
    op.drop_table("users")
    op.drop_table("units")
    op.drop_table("roles")
    op.drop_table("departments")
    op.drop_table("currencies")


# --------------------------------------------------------------------------- защита данных на уровне БД
# Даже при ошибке в коде приложения проводки нельзя изменить или удалить, а в закрытый период — записать.

APPEND_ONLY = ("ledger_entries", "stock_moves", "tariffs", "audit_log", "period_statements", "period_reopenings")
NO_DELETE = ("stock_documents", "stock_document_lines", "discrepancy_cases", "payroll_periods")
MONEY_DOCS = ("adjustments", "payouts", "payout_requests")

GUARD_FUNCTIONS = """
CREATE FUNCTION bugalter_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Таблица % ведётся только добавлением: % запрещён', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'P0001';
END $$;

CREATE FUNCTION bugalter_forbid_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Из таблицы % нельзя удалять записи', TG_TABLE_NAME USING ERRCODE = 'P0001';
END $$;

CREATE FUNCTION bugalter_ledger_period_open() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    period_status text;
BEGIN
    -- FOR SHARE: закрытие периода (UPDATE payroll_periods) дождётся завершения этой транзакции.
    SELECT status INTO period_status FROM payroll_periods WHERE id = NEW.period_id FOR SHARE;
    IF period_status IS DISTINCT FROM 'open' THEN
        RAISE EXCEPTION 'Расчётный период % закрыт', NEW.period_id USING ERRCODE = 'P0002';
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION bugalter_work_entry_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Операции нельзя удалять, только отменять' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status IN ('confirmed', 'rejected', 'cancelled') THEN
        IF (NEW.employee_id, NEW.work_type_id, NEW.department_id, NEW.quantity, NEW.rate, NEW.amount,
            NEW.currency, NEW.performed_on, NEW.tariff_id)
           IS DISTINCT FROM
           (OLD.employee_id, OLD.work_type_id, OLD.department_id, OLD.quantity, OLD.rate, OLD.amount,
            OLD.currency, OLD.performed_on, OLD.tariff_id) THEN
            RAISE EXCEPTION 'Операцию % в статусе % нельзя изменить', OLD.id, OLD.status USING ERRCODE = 'P0001';
        END IF;
        IF OLD.status IN ('rejected', 'cancelled') AND NEW.status <> OLD.status THEN
            RAISE EXCEPTION 'Операция % уже закрыта (%)', OLD.id, OLD.status USING ERRCODE = 'P0001';
        END IF;
        IF OLD.status = 'confirmed' AND NEW.status NOT IN ('confirmed', 'cancelled') THEN
            RAISE EXCEPTION 'Подтверждённую операцию % можно только отменить', OLD.id USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION bugalter_money_doc_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Финансовые документы (%) нельзя удалять', TG_TABLE_NAME USING ERRCODE = 'P0001';
    END IF;
    IF (NEW.employee_id, NEW.amount, NEW.currency) IS DISTINCT FROM (OLD.employee_id, OLD.amount, OLD.currency) THEN
        RAISE EXCEPTION 'Сумму, валюту и сотрудника в документе %/% менять нельзя', TG_TABLE_NAME, OLD.id
            USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status IN ('rejected', 'cancelled') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'Документ %/% уже закрыт', TG_TABLE_NAME, OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status IN ('approved', 'confirmed') AND NEW.status NOT IN (OLD.status, 'cancelled') THEN
        RAISE EXCEPTION 'Проведённый документ %/% можно только отменить', TG_TABLE_NAME, OLD.id
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;
"""


def install_guards() -> None:
    # asyncpg не принимает несколько команд в одном запросе — выполняем функции по одной.
    for statement in GUARD_FUNCTIONS.split("\n\n"):
        op.execute(statement)
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bugalter_forbid_change()"
        )
    for table in NO_DELETE:
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bugalter_forbid_delete()"
        )
    for table in MONEY_DOCS:
        op.execute(
            f"CREATE TRIGGER trg_{table}_guard BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bugalter_money_doc_guard()"
        )
    op.execute(
        "CREATE TRIGGER trg_work_entries_guard BEFORE UPDATE OR DELETE ON work_entries "
        "FOR EACH ROW EXECUTE FUNCTION bugalter_work_entry_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_ledger_period_open BEFORE INSERT ON ledger_entries "
        "FOR EACH ROW EXECUTE FUNCTION bugalter_ledger_period_open()"
    )


def drop_guards() -> None:
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    for table in NO_DELETE:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete ON {table}")
    for table in MONEY_DOCS:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_guard ON {table}")
    op.execute("DROP TRIGGER IF EXISTS trg_work_entries_guard ON work_entries")
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_period_open ON ledger_entries")
    for fn in (
        "bugalter_forbid_change",
        "bugalter_forbid_delete",
        "bugalter_ledger_period_open",
        "bugalter_work_entry_guard",
        "bugalter_money_doc_guard",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")
