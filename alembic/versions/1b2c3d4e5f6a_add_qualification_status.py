"""add qualification status

Revision ID: 1b2c3d4e5f6a
Revises: 0a1b2c3d4e5f
Create Date: 2026-06-21 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "1b2c3d4e5f6a"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index["name"] == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def upgrade() -> None:
    if not _table_exists("opportunities"):
        return

    if not _has_column("opportunities", "qualification_status"):
        op.add_column(
            "opportunities",
            sa.Column(
                "qualification_status",
                sa.String(),
                nullable=False,
                server_default="unreviewed",
            ),
        )

    op.execute(
        sa.text(
            """
            UPDATE opportunities
            SET qualification_status = 'qualified'
            WHERE qualification_status IS NULL
               OR qualification_status = ''
               OR qualification_status = 'unreviewed'
            """
        )
    )

    if not _index_exists("opportunities", "ix_opportunities_qualification_status"):
        op.create_index(
            op.f("ix_opportunities_qualification_status"),
            "opportunities",
            ["qualification_status"],
            unique=False,
        )


def downgrade() -> None:
    if not _table_exists("opportunities"):
        return

    if _index_exists("opportunities", "ix_opportunities_qualification_status"):
        op.drop_index(op.f("ix_opportunities_qualification_status"), table_name="opportunities")
    if _has_column("opportunities", "qualification_status"):
        op.drop_column("opportunities", "qualification_status")
