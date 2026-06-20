"""add account type metadata

Revision ID: 0a1b2c3d4e5f
Revises: f9d0e1f2a3b4
Create Date: 2026-06-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0a1b2c3d4e5f"
down_revision = "f9d0e1f2a3b4"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if not _table_exists("opportunities"):
        return

    if not _has_column("opportunities", "account_type"):
        op.add_column("opportunities", sa.Column("account_type", sa.String(), nullable=True))
    if not _has_column("opportunities", "account_type_confidence"):
        op.add_column("opportunities", sa.Column("account_type_confidence", sa.String(), nullable=True))
    if not _has_column("opportunities", "account_type_source"):
        op.add_column("opportunities", sa.Column("account_type_source", sa.String(), nullable=True))


def downgrade() -> None:
    if not _table_exists("opportunities"):
        return

    if _has_column("opportunities", "account_type_source"):
        op.drop_column("opportunities", "account_type_source")
    if _has_column("opportunities", "account_type_confidence"):
        op.drop_column("opportunities", "account_type_confidence")
    if _has_column("opportunities", "account_type"):
        op.drop_column("opportunities", "account_type")
