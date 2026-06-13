"""add company profile archive

Revision ID: f4a5b6c7d8e9
Revises: e2f3a4b5c6d7
Create Date: 2026-06-09 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f4a5b6c7d8e9"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    if table_name not in sa.inspect(bind).get_table_names():
        return False
    return column_name in {col["name"] for col in sa.inspect(bind).get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("company_profiles", "archived_at"):
        op.add_column("company_profiles", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index(op.f("ix_company_profiles_archived_at"), "company_profiles", ["archived_at"], unique=False)


def downgrade() -> None:
    if _has_column("company_profiles", "archived_at"):
        op.drop_index(op.f("ix_company_profiles_archived_at"), table_name="company_profiles")
        op.drop_column("company_profiles", "archived_at")
