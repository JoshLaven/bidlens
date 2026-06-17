"""make sam url nullable

Revision ID: f7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-16 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("opportunities", "sam_url"):
        return

    with op.batch_alter_table("opportunities") as batch_op:
        batch_op.alter_column("sam_url", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    if not _has_column("opportunities", "sam_url"):
        return

    op.execute(
        sa.text(
            """
            UPDATE opportunities
            SET sam_url = COALESCE(NULLIF(sam_url, ''), source_url, '')
            WHERE sam_url IS NULL
            """
        )
    )
    with op.batch_alter_table("opportunities") as batch_op:
        batch_op.alter_column("sam_url", existing_type=sa.String(), nullable=False)
