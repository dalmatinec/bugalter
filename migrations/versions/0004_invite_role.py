"""invite role

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12 10:23:48.108395
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("employee_invites", sa.Column("role_code", sa.String(length=32), nullable=True))
    op.create_foreign_key(
        op.f("fk_employee_invites_role_code_roles"), "employee_invites", "roles", ["role_code"], ["code"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_employee_invites_role_code_roles"), "employee_invites", type_="foreignkey")
    op.drop_column("employee_invites", "role_code")
