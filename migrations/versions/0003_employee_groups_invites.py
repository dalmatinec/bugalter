"""employee groups and invites

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11 01:54:11.947356
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "employee_invites",
        sa.Column("token", sa.String(length=40), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_employee_invites_created_by_users")),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name=op.f("fk_employee_invites_employee_id_employees")
        ),
        sa.ForeignKeyConstraint(["used_by"], ["users.id"], name=op.f("fk_employee_invites_used_by_users")),
        sa.PrimaryKeyConstraint("token", name=op.f("pk_employee_invites")),
    )
    op.add_column("telegram_chats", sa.Column("employee_id", sa.Integer(), nullable=True))
    op.create_index(
        "ix_telegram_chats_employee",
        "telegram_chats",
        ["employee_id"],
        unique=False,
        postgresql_where=sa.text("employee_id IS NOT NULL"),
    )
    op.create_foreign_key(
        op.f("fk_telegram_chats_employee_id_employees"), "telegram_chats", "employees", ["employee_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_telegram_chats_employee_id_employees"), "telegram_chats", type_="foreignkey")
    op.drop_index(
        "ix_telegram_chats_employee", table_name="telegram_chats", postgresql_where=sa.text("employee_id IS NOT NULL")
    )
    op.drop_column("telegram_chats", "employee_id")
    op.drop_table("employee_invites")
