"""product in work reports, expenses

work_entries: какой товар и сколько штук какого веса указал сотрудник («Крам факт 0.5 13»),
чтобы считать статистику по товарам без разбора комментариев.
expenses: расходы компании (реклама, сотрудничество, аренда и т. п.).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_entries", sa.Column("product_name", sa.String(length=120), nullable=True))
    op.add_column("work_entries", sa.Column("pieces", sa.Numeric(precision=20, scale=6), nullable=True))
    op.add_column("work_entries", sa.Column("piece_weight", sa.Numeric(precision=20, scale=6), nullable=True))
    op.create_index("ix_work_entries_product_name", "work_entries", ["product_name"])
    op.create_table(
        "expenses",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=8), sa.ForeignKey("currencies.code"), nullable=False),
        sa.Column("spent_on", sa.Date(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_by", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_expenses_amount_positive"),
    )
    op.create_index("ix_expenses_spent_on", "expenses", ["spent_on"])


def downgrade() -> None:
    op.drop_index("ix_expenses_spent_on", table_name="expenses")
    op.drop_table("expenses")
    op.drop_index("ix_work_entries_product_name", table_name="work_entries")
    op.drop_column("work_entries", "piece_weight")
    op.drop_column("work_entries", "pieces")
    op.drop_column("work_entries", "product_name")
