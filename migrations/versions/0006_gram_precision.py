"""grams with fractions

Работа по весу: «0,5 г × 3 шт» даёт дробные граммы, поэтому грамм считается до 3 знаков.
Точность только растёт, старые записи остаются верными.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17 10:00:00
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE units SET scale = 3 WHERE code = 'г' AND scale < 3")


def downgrade() -> None:
    pass
