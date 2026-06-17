"""add crm push status

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-16 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _has_column("opportunities", "crm_pushed"):
        op.add_column(
            "opportunities",
            sa.Column("crm_pushed", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if not _has_column("opportunities", "crm_pushed_at"):
        op.add_column("opportunities", sa.Column("crm_pushed_at", sa.DateTime(), nullable=True))
    if not _has_column("opportunities", "crm_pushed_by"):
        op.add_column("opportunities", sa.Column("crm_pushed_by", sa.Integer(), nullable=True))

    if not _index_exists("opportunities", "ix_opportunities_crm_pushed"):
        op.create_index(op.f("ix_opportunities_crm_pushed"), "opportunities", ["crm_pushed"], unique=False)


def downgrade() -> None:
    if _index_exists("opportunities", "ix_opportunities_crm_pushed"):
        op.drop_index(op.f("ix_opportunities_crm_pushed"), table_name="opportunities")
    if _has_column("opportunities", "crm_pushed_by"):
        op.drop_column("opportunities", "crm_pushed_by")
    if _has_column("opportunities", "crm_pushed_at"):
        op.drop_column("opportunities", "crm_pushed_at")
    if _has_column("opportunities", "crm_pushed"):
        op.drop_column("opportunities", "crm_pushed")
