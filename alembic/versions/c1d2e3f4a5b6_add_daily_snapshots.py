"""add daily snapshots

Revision ID: c1d2e3f4a5b6
Revises: b7c8d9e0f1a2
"""

from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b6"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "daily_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="completed"),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id", "snapshot_date", name="uq_daily_snapshot_workspace_user_date"),
    )
    op.create_index(op.f("ix_daily_snapshots_id"), "daily_snapshots", ["id"], unique=False)
    op.create_index(op.f("ix_daily_snapshots_workspace_id"), "daily_snapshots", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_daily_snapshots_user_id"), "daily_snapshots", ["user_id"], unique=False)
    op.create_index(op.f("ix_daily_snapshots_snapshot_date"), "daily_snapshots", ["snapshot_date"], unique=False)
    op.create_index(op.f("ix_daily_snapshots_status"), "daily_snapshots", ["status"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_daily_snapshots_status"), table_name="daily_snapshots")
    op.drop_index(op.f("ix_daily_snapshots_snapshot_date"), table_name="daily_snapshots")
    op.drop_index(op.f("ix_daily_snapshots_user_id"), table_name="daily_snapshots")
    op.drop_index(op.f("ix_daily_snapshots_workspace_id"), table_name="daily_snapshots")
    op.drop_index(op.f("ix_daily_snapshots_id"), table_name="daily_snapshots")
    op.drop_table("daily_snapshots")
