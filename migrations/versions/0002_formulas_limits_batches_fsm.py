"""pricing formulas, withholding limits, credit, batches, fsm, indexes

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11 01:20:12.874404
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fsm_states",
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=200), nullable=True),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_fsm_states")),
    )
    op.create_index("ix_fsm_states_updated", "fsm_states", ["updated_at"], unique=False)
    op.create_table(
        "coefficients",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("factor", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("factor > 0", name=op.f("ck_coefficients_factor_positive")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_coefficients_created_by_users")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_coefficients")),
    )
    op.create_table(
        "employee_credit_limits",
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("set_by", sa.BigInteger(), nullable=True),
        sa.Column("set_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_employee_credit_limits_amount_non_negative")),
        sa.ForeignKeyConstraint(
            ["currency"], ["currencies.code"], name=op.f("fk_employee_credit_limits_currency_currencies")
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_employee_credit_limits_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(["set_by"], ["users.id"], name=op.f("fk_employee_credit_limits_set_by_users")),
        sa.PrimaryKeyConstraint("employee_id", "currency", name=op.f("pk_employee_credit_limits")),
    )
    op.create_table(
        "adjustment_installments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("adjustment_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_adjustment_installments_amount_positive")),
        sa.ForeignKeyConstraint(
            ["adjustment_id"], ["adjustments.id"], name=op.f("fk_adjustment_installments_adjustment_id_adjustments")
        ),
        sa.ForeignKeyConstraint(
            ["period_id"], ["payroll_periods.id"], name=op.f("fk_adjustment_installments_period_id_payroll_periods")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_adjustment_installments")),
        sa.UniqueConstraint("adjustment_id", "seq", name=op.f("uq_adjustment_installments_adjustment_id_seq")),
    )
    op.create_table(
        "tariff_tiers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tariff_id", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("rate", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.CheckConstraint("rate >= 0", name=op.f("ck_tariff_tiers_rate_non_negative")),
        sa.CheckConstraint("threshold > 0", name=op.f("ck_tariff_tiers_threshold_positive")),
        sa.ForeignKeyConstraint(["tariff_id"], ["tariffs.id"], name=op.f("fk_tariff_tiers_tariff_id_tariffs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tariff_tiers")),
        sa.UniqueConstraint("tariff_id", "threshold", name=op.f("uq_tariff_tiers_tariff_id_threshold")),
    )
    op.add_column(
        "adjustments",
        sa.Column("withheld", sa.Numeric(precision=20, scale=4), server_default=sa.text("0"), nullable=False),
    )
    op.create_index("ix_adjustments_date", "adjustments", ["business_date"], unique=False)
    op.create_index("ix_adjustments_employee", "adjustments", ["employee_id", "business_date"], unique=False)
    op.create_index(
        "ix_adjustments_open",
        "adjustments",
        ["department_id"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'awaiting_consent', 'disputed')"),
    )
    op.create_index(
        "ix_adjustments_outstanding",
        "adjustments",
        ["employee_id", "currency"],
        unique=False,
        postgresql_where=sa.text("status = 'approved' AND kind <> 'bonus' AND withheld < amount"),
    )
    op.create_index("ix_audit_actor", "audit_log", ["actor_user_id", "at"], unique=False)
    op.add_column("batches", sa.Column("department_id", sa.Integer(), nullable=True))
    op.add_column("batches", sa.Column("document_id", sa.BigInteger(), nullable=True))
    op.add_column("batches", sa.Column("qty_received", sa.Numeric(precision=20, scale=6), nullable=True))
    op.alter_column(
        "batches",
        "unit_cost",
        existing_type=sa.NUMERIC(precision=20, scale=4),
        type_=sa.Numeric(precision=20, scale=6),
        existing_nullable=True,
    )
    op.create_index("ix_batches_fifo", "batches", ["product_id", "received_on", "id"], unique=False)
    op.create_foreign_key(
        op.f("fk_batches_department_id_departments"), "batches", "departments", ["department_id"], ["id"]
    )
    op.create_foreign_key(
        op.f("fk_batches_document_id_stock_documents"), "batches", "stock_documents", ["document_id"], ["id"]
    )
    op.alter_column(
        "discrepancy_cases",
        "price",
        existing_type=sa.NUMERIC(precision=20, scale=4),
        type_=sa.Numeric(precision=20, scale=6),
        existing_nullable=True,
    )
    op.create_index(
        "ix_discrepancy_cases_open",
        "discrepancy_cases",
        ["department_id"],
        unique=False,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_employee_departments_dept",
        "employee_departments",
        ["department_id"],
        unique=False,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index("ix_ledger_dept_date", "ledger_entries", ["department_id", "business_date"], unique=False)
    op.create_index("ix_ledger_employee_date", "ledger_entries", ["employee_id", "business_date"], unique=False)
    op.create_index("ix_ledger_employee_period", "ledger_entries", ["employee_id", "period_id"], unique=False)
    op.create_index(
        "ix_ledger_reverses",
        "ledger_entries",
        ["reverses_id"],
        unique=False,
        postgresql_where=sa.text("reverses_id IS NOT NULL"),
    )
    op.create_index(
        "ix_locations_employee",
        "locations",
        ["employee_id"],
        unique=False,
        postgresql_where=sa.text("employee_id IS NOT NULL"),
    )
    op.create_index(
        "ix_payout_requests_open",
        "payout_requests",
        ["employee_id"],
        unique=False,
        postgresql_where=sa.text("status = 'requested'"),
    )
    op.create_index("ix_payouts_date", "payouts", ["business_date"], unique=False)
    op.create_index("ix_payouts_employee", "payouts", ["employee_id", "business_date"], unique=False)
    op.create_index(
        "ix_payouts_registered",
        "payouts",
        ["employee_id"],
        unique=False,
        postgresql_where=sa.text("status = 'registered'"),
    )
    op.create_index("ix_stock_balances_product", "stock_balances", ["product_id"], unique=False)
    op.create_index("ix_stock_balances_location_product", "stock_balances", ["location_id", "product_id"], unique=False)
    op.create_index("ix_stock_document_lines_document", "stock_document_lines", ["document_id"], unique=False)
    op.create_index(
        "ix_stock_documents_in_transit",
        "stock_documents",
        ["id"],
        unique=False,
        postgresql_where=sa.text("status = 'in_transit'"),
    )
    op.create_index(
        "ix_stock_documents_target_dept",
        "stock_documents",
        ["target_department_id"],
        unique=False,
        postgresql_where=sa.text("target_department_id IS NOT NULL"),
    )
    op.create_index("ix_stock_moves_document", "stock_moves", ["document_id"], unique=False)
    op.create_index("ix_stock_moves_posted_at", "stock_moves", ["posted_at"], unique=False)
    op.drop_index("ix_tariffs_lookup", table_name="tariffs")
    op.create_index(
        "ix_tariffs_lookup", "tariffs", ["work_type_id", "employee_id", "department_id", "valid_from"], unique=False
    )
    op.create_index(
        "ix_telegram_chats_dept",
        "telegram_chats",
        ["department_id"],
        unique=False,
        postgresql_where=sa.text("is_active"),
    )
    op.add_column(
        "work_entries",
        sa.Column("coefficient", sa.Numeric(precision=10, scale=4), server_default=sa.text("1"), nullable=False),
    )
    op.add_column("work_entries", sa.Column("coefficient_codes", postgresql.ARRAY(sa.String(length=32)), nullable=True))
    op.add_column("work_entries", sa.Column("calc_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.drop_index("ix_work_entries_status", table_name="work_entries")
    op.create_index(
        "ix_work_entries_confirmed_volume",
        "work_entries",
        ["employee_id", "work_type_id", "department_id", "performed_on"],
        unique=False,
        postgresql_where=sa.text("status = 'confirmed'"),
    )
    op.create_index(
        "ix_work_entries_open",
        "work_entries",
        ["department_id", "work_type_id", "performed_on"],
        unique=False,
        postgresql_where=sa.text("status IN ('draft', 'pending')"),
    )

    upgrade_rules()


def downgrade() -> None:
    downgrade_rules()
    op.drop_index(
        "ix_work_entries_open", table_name="work_entries", postgresql_where=sa.text("status IN ('draft', 'pending')")
    )
    op.drop_index(
        "ix_work_entries_confirmed_volume", table_name="work_entries", postgresql_where=sa.text("status = 'confirmed'")
    )
    op.create_index("ix_work_entries_status", "work_entries", ["status"], unique=False)
    op.drop_column("work_entries", "calc_details")
    op.drop_column("work_entries", "coefficient_codes")
    op.drop_column("work_entries", "coefficient")
    op.drop_index("ix_telegram_chats_dept", table_name="telegram_chats", postgresql_where=sa.text("is_active"))
    op.drop_index("ix_tariffs_lookup", table_name="tariffs")
    op.create_index("ix_tariffs_lookup", "tariffs", ["work_type_id", "valid_from"], unique=False)
    op.drop_index("ix_stock_moves_posted_at", table_name="stock_moves")
    op.drop_index("ix_stock_moves_document", table_name="stock_moves")
    op.drop_index(
        "ix_stock_documents_target_dept",
        table_name="stock_documents",
        postgresql_where=sa.text("target_department_id IS NOT NULL"),
    )
    op.drop_index(
        "ix_stock_documents_in_transit", table_name="stock_documents", postgresql_where=sa.text("status = 'in_transit'")
    )
    op.drop_index("ix_stock_document_lines_document", table_name="stock_document_lines")
    op.drop_index("ix_stock_balances_location_product", table_name="stock_balances", if_exists=True)
    op.drop_index("ix_stock_balances_product", table_name="stock_balances")
    op.drop_index("ix_payouts_registered", table_name="payouts", postgresql_where=sa.text("status = 'registered'"))
    op.drop_index("ix_payouts_employee", table_name="payouts")
    op.drop_index("ix_payouts_date", table_name="payouts")
    op.drop_index(
        "ix_payout_requests_open", table_name="payout_requests", postgresql_where=sa.text("status = 'requested'")
    )
    op.drop_index("ix_locations_employee", table_name="locations", postgresql_where=sa.text("employee_id IS NOT NULL"))
    op.drop_index(
        "ix_ledger_reverses", table_name="ledger_entries", postgresql_where=sa.text("reverses_id IS NOT NULL")
    )
    op.drop_index("ix_ledger_employee_period", table_name="ledger_entries")
    op.drop_index("ix_ledger_employee_date", table_name="ledger_entries")
    op.drop_index("ix_ledger_dept_date", table_name="ledger_entries")
    op.drop_index(
        "ix_employee_departments_dept", table_name="employee_departments", postgresql_where=sa.text("is_active")
    )
    op.drop_index(
        "ix_discrepancy_cases_open", table_name="discrepancy_cases", postgresql_where=sa.text("status = 'open'")
    )
    op.alter_column(
        "discrepancy_cases",
        "price",
        existing_type=sa.Numeric(precision=20, scale=6),
        type_=sa.NUMERIC(precision=20, scale=4),
        existing_nullable=True,
    )
    op.drop_constraint(op.f("fk_batches_document_id_stock_documents"), "batches", type_="foreignkey")
    op.drop_constraint(op.f("fk_batches_department_id_departments"), "batches", type_="foreignkey")
    op.drop_index("ix_batches_fifo", table_name="batches")
    op.alter_column(
        "batches",
        "unit_cost",
        existing_type=sa.Numeric(precision=20, scale=6),
        type_=sa.NUMERIC(precision=20, scale=4),
        existing_nullable=True,
    )
    op.drop_column("batches", "qty_received")
    op.drop_column("batches", "document_id")
    op.drop_column("batches", "department_id")
    op.drop_index("ix_audit_actor", table_name="audit_log")
    op.drop_index(
        "ix_adjustments_outstanding",
        table_name="adjustments",
        postgresql_where=sa.text("status = 'approved' AND kind <> 'bonus' AND withheld < amount"),
    )
    op.drop_index(
        "ix_adjustments_open",
        table_name="adjustments",
        postgresql_where=sa.text("status IN ('pending', 'awaiting_consent', 'disputed')"),
    )
    op.drop_index("ix_adjustments_employee", table_name="adjustments")
    op.drop_index("ix_adjustments_date", table_name="adjustments")
    op.drop_column("adjustments", "withheld")
    op.drop_table("tariff_tiers")
    op.drop_table("adjustment_installments")
    op.drop_table("employee_credit_limits")
    op.drop_table("coefficients")
    op.drop_index("ix_fsm_states_updated", table_name="fsm_states")
    op.drop_table("fsm_states")


# --------------------------------------------------------------------------- правила и данные

APPEND_ONLY = ("tariff_tiers", "adjustment_installments", "batches")

WORK_ENTRY_GUARD = """
CREATE OR REPLACE FUNCTION bugalter_work_entry_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Операции нельзя удалять, только отменять' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status IN ('confirmed', 'rejected', 'cancelled') THEN
        IF (NEW.employee_id, NEW.work_type_id, NEW.department_id, NEW.quantity, NEW.rate, NEW.amount,
            NEW.currency, NEW.performed_on, NEW.tariff_id, NEW.coefficient, NEW.calc_details)
           IS DISTINCT FROM
           (OLD.employee_id, OLD.work_type_id, OLD.department_id, OLD.quantity, OLD.rate, OLD.amount,
            OLD.currency, OLD.performed_on, OLD.tariff_id, OLD.coefficient, OLD.calc_details) THEN
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
END $$
"""

WORK_ENTRY_GUARD_V1 = WORK_ENTRY_GUARD.replace(", NEW.coefficient, NEW.calc_details", "").replace(
    ", OLD.coefficient, OLD.calc_details", ""
)


def upgrade_rules() -> None:
    op.drop_constraint(op.f("ck_work_types_calc_method"), "work_types", type_="check")
    op.create_check_constraint("calc_method", "work_types", "calc_method IN ('per_unit', 'fixed', 'percent')")
    op.create_check_constraint("coefficient_positive", "work_entries", "coefficient > 0")
    op.create_check_constraint("withheld_range", "adjustments", "withheld >= 0 AND withheld <= amount")
    op.create_check_constraint("unit_cost_non_negative", "batches", "unit_cost IS NULL OR unit_cost >= 0")
    op.create_check_constraint("cost_has_currency", "batches", "(unit_cost IS NULL) = (currency IS NULL)")
    # Удержания, проведённые до появления лимита, проведены целиком.
    op.execute("UPDATE adjustments SET withheld = amount WHERE status = 'approved' AND kind <> 'bonus'")
    op.execute(WORK_ENTRY_GUARD)
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bugalter_forbid_change()"
        )


def downgrade_rules() -> None:
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute(WORK_ENTRY_GUARD_V1)
    op.drop_constraint(op.f("ck_batches_cost_has_currency"), "batches", type_="check")
    op.drop_constraint(op.f("ck_batches_unit_cost_non_negative"), "batches", type_="check")
    op.drop_constraint(op.f("ck_adjustments_withheld_range"), "adjustments", type_="check")
    op.drop_constraint(op.f("ck_work_entries_coefficient_positive"), "work_entries", type_="check")
    op.drop_constraint(op.f("ck_work_types_calc_method"), "work_types", type_="check")
    op.create_check_constraint("calc_method", "work_types", "calc_method IN ('per_unit', 'fixed')")
