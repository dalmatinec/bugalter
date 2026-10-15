"""advance settlement instead of credit limits

Аванс теперь любой суммы; при зарплате руководитель решает, вычесть его сейчас или в следующий раз.
payouts.settled: сколько из этого аванса уже вычтено из зарплаты. Лимиты «аванса в долг» больше не нужны.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payouts",
        sa.Column("settled", sa.Numeric(precision=20, scale=4), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(op.f("ck_payouts_settled_range"), "payouts", "settled >= 0 AND settled <= amount")
    op.drop_table("employee_credit_limits")
    op.execute("DELETE FROM settings WHERE key = 'default_credit_limit'")


def downgrade() -> None:
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
    op.drop_constraint(op.f("ck_payouts_settled_range"), "payouts", type_="check")
    op.drop_column("payouts", "settled")
