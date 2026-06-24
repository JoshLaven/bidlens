"""extend ingestion runs for import history

Revision ID: 5f6a7b8c9d0e
Revises: 4e5f6a7b8c9d
Create Date: 2026-06-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "5f6a7b8c9d0e"
down_revision = "4e5f6a7b8c9d"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if not _table_exists("ingestion_runs"):
        return

    columns = {
        "organization_id": sa.Column("organization_id", sa.Integer(), nullable=True),
        "user_id": sa.Column("user_id", sa.Integer(), nullable=True),
        "filename": sa.Column("filename", sa.String(), nullable=True),
        "processed_count": sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        "created_count": sa.Column("created_count", sa.Integer(), nullable=False, server_default="0"),
        "updated_count": sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
        "unchanged_count": sa.Column("unchanged_count", sa.Integer(), nullable=False, server_default="0"),
        "reason_summary_json": sa.Column("reason_summary_json", sa.JSON(), nullable=True),
    }
    for name, column in columns.items():
        if not _has_column("ingestion_runs", name):
            op.add_column("ingestion_runs", column)

    indexes = {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes("ingestion_runs")}
    if "ix_ingestion_runs_organization_id" not in indexes:
        op.create_index("ix_ingestion_runs_organization_id", "ingestion_runs", ["organization_id"], unique=False)
    if "ix_ingestion_runs_user_id" not in indexes:
        op.create_index("ix_ingestion_runs_user_id", "ingestion_runs", ["user_id"], unique=False)


def downgrade() -> None:
    if not _table_exists("ingestion_runs"):
        return

    indexes = {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes("ingestion_runs")}
    if "ix_ingestion_runs_user_id" in indexes:
        op.drop_index("ix_ingestion_runs_user_id", table_name="ingestion_runs")
    if "ix_ingestion_runs_organization_id" in indexes:
        op.drop_index("ix_ingestion_runs_organization_id", table_name="ingestion_runs")

    for name in (
        "reason_summary_json",
        "unchanged_count",
        "updated_count",
        "created_count",
        "processed_count",
        "filename",
        "user_id",
        "organization_id",
    ):
        if _has_column("ingestion_runs", name):
            op.drop_column("ingestion_runs", name)
