"""add salesforce sync metadata

Revision ID: 4e5f6a7b8c9d
Revises: 3d4e5f6a7b8c
Create Date: 2026-06-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "4e5f6a7b8c9d"
down_revision = "3d4e5f6a7b8c"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("opportunities", "salesforce_synced_at"):
        op.add_column("opportunities", sa.Column("salesforce_synced_at", sa.DateTime(), nullable=True))
    if not _has_column("opportunities", "salesforce_action"):
        op.add_column("opportunities", sa.Column("salesforce_action", sa.String(), nullable=True))


def downgrade() -> None:
    if _has_column("opportunities", "salesforce_action"):
        op.drop_column("opportunities", "salesforce_action")
    if _has_column("opportunities", "salesforce_synced_at"):
        op.drop_column("opportunities", "salesforce_synced_at")
