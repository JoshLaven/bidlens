"""add salesforce opportunity reference

Revision ID: 3d4e5f6a7b8c
Revises: 2c3d4e5f6a7b
Create Date: 2026-06-22 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "3d4e5f6a7b8c"
down_revision = "2c3d4e5f6a7b"
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
    if not _has_column("opportunities", "salesforce_opportunity_id"):
        op.add_column("opportunities", sa.Column("salesforce_opportunity_id", sa.String(), nullable=True))
    if not _has_column("opportunities", "salesforce_opportunity_url"):
        op.add_column("opportunities", sa.Column("salesforce_opportunity_url", sa.String(), nullable=True))
    if not _index_exists("opportunities", "ix_opportunities_salesforce_opportunity_id"):
        op.create_index(
            op.f("ix_opportunities_salesforce_opportunity_id"),
            "opportunities",
            ["salesforce_opportunity_id"],
            unique=False,
        )


def downgrade() -> None:
    if _index_exists("opportunities", "ix_opportunities_salesforce_opportunity_id"):
        op.drop_index(op.f("ix_opportunities_salesforce_opportunity_id"), table_name="opportunities")
    if _has_column("opportunities", "salesforce_opportunity_url"):
        op.drop_column("opportunities", "salesforce_opportunity_url")
    if _has_column("opportunities", "salesforce_opportunity_id"):
        op.drop_column("opportunities", "salesforce_opportunity_id")
