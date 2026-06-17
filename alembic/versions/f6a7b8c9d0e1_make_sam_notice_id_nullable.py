"""make sam notice id nullable

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-16 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("opportunities", "sam_notice_id"):
        return

    with op.batch_alter_table("opportunities") as batch_op:
        batch_op.alter_column("sam_notice_id", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    if not _has_column("opportunities", "sam_notice_id"):
        return

    op.execute(
        sa.text(
            """
            UPDATE opportunities
            SET sam_notice_id = COALESCE(NULLIF(sam_notice_id, ''), source_record_id, 'legacy-' || id)
            WHERE sam_notice_id IS NULL OR sam_notice_id = ''
            """
        )
    )
    with op.batch_alter_table("opportunities") as batch_op:
        batch_op.alter_column("sam_notice_id", existing_type=sa.String(), nullable=False)
