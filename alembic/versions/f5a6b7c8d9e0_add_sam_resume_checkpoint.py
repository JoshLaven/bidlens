"""add SAM resume checkpoint

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
"""

from alembic import op
import sqlalchemy as sa


revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ingestion_runs",
        sa.Column("source_config_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="completed",
        ),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("retry_after_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("checkpoint_json", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_ingestion_runs_source_config_id",
        "ingestion_runs",
        ["source_config_id"],
        unique=False,
    )
    op.create_index(
        "ix_ingestion_runs_status",
        "ingestion_runs",
        ["status"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_ingestion_runs_status", table_name="ingestion_runs")
    op.drop_index("ix_ingestion_runs_source_config_id", table_name="ingestion_runs")
    op.drop_column("ingestion_runs", "checkpoint_json")
    op.drop_column("ingestion_runs", "retry_after_at")
    op.drop_column("ingestion_runs", "status")
    op.drop_column("ingestion_runs", "source_config_id")
