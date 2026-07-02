"""add ingestion run details

Revision ID: 8e9f0a1b2c3d
Revises: 7c8d9e0f1a2b
"""

from alembic import op
import sqlalchemy as sa


revision = "8e9f0a1b2c3d"
down_revision = "7c8d9e0f1a2b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ingestion_run_details",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ingestion_run_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_record_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("result", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("matched_opportunity_id", sa.Integer(), nullable=True),
        sa.Column("changed_fields_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"]),
        sa.ForeignKeyConstraint(["matched_opportunity_id"], ["opportunities.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "id",
        "ingestion_run_id",
        "source",
        "source_record_id",
        "result",
        "matched_opportunity_id",
    ):
        op.create_index(
            op.f(f"ix_ingestion_run_details_{column}"),
            "ingestion_run_details",
            [column],
            unique=False,
        )


def downgrade():
    for column in (
        "matched_opportunity_id",
        "result",
        "source_record_id",
        "source",
        "ingestion_run_id",
        "id",
    ):
        op.drop_index(
            op.f(f"ix_ingestion_run_details_{column}"),
            table_name="ingestion_run_details",
        )
    op.drop_table("ingestion_run_details")
