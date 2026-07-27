"""add opportunity communication summaries

Revision ID: 1a2b3c4d5e6f
Revises: 0f1e2d3c4b5a
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa

revision = "1a2b3c4d5e6f"
down_revision = "0f1e2d3c4b5a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunity_communication_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), server_default="ready", nullable=False),
        sa.Column("current_status", sa.Text(), nullable=True),
        sa.Column("key_updates_json", sa.JSON(), nullable=True),
        sa.Column("open_questions_json", sa.JSON(), nullable=True),
        sa.Column("next_action", sa.Text(), nullable=True),
        sa.Column("waiting_on", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("message_count_included", sa.Integer(), server_default="0", nullable=False),
        sa.Column("message_count_available", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latest_message_timestamp_included", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["generated_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "opportunity_id", name="uq_opp_comm_summary_workspace_opportunity"),
    )
    for column in ("id", "workspace_id", "organization_id", "opportunity_id", "status", "generated_by_user_id"):
        op.create_index(op.f(f"ix_opportunity_communication_summaries_{column}"), "opportunity_communication_summaries", [column], unique=False)


def downgrade() -> None:
    op.drop_table("opportunity_communication_summaries")
