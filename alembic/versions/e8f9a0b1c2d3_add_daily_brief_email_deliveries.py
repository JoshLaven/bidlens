"""add daily brief email deliveries

Revision ID: e8f9a0b1c2d3
Revises: e7f8a9b0c1d2
Create Date: 2026-07-22
"""

from alembic import op
import sqlalchemy as sa


revision = "e8f9a0b1c2d3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "daily_brief_email_opted_out",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "daily_brief_email_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("recipient_email", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("item_count", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id", "snapshot_date", name="uq_daily_brief_delivery_workspace_user_date"),
    )
    op.create_index(
        op.f("ix_daily_brief_email_deliveries_id"),
        "daily_brief_email_deliveries",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_daily_brief_email_deliveries_organization_id"),
        "daily_brief_email_deliveries",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_daily_brief_email_deliveries_snapshot_date"),
        "daily_brief_email_deliveries",
        ["snapshot_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_daily_brief_email_deliveries_status"),
        "daily_brief_email_deliveries",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_daily_brief_email_deliveries_user_id"),
        "daily_brief_email_deliveries",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_daily_brief_email_deliveries_workspace_id"),
        "daily_brief_email_deliveries",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_daily_brief_email_deliveries_workspace_id"), table_name="daily_brief_email_deliveries")
    op.drop_index(op.f("ix_daily_brief_email_deliveries_user_id"), table_name="daily_brief_email_deliveries")
    op.drop_index(op.f("ix_daily_brief_email_deliveries_status"), table_name="daily_brief_email_deliveries")
    op.drop_index(op.f("ix_daily_brief_email_deliveries_snapshot_date"), table_name="daily_brief_email_deliveries")
    op.drop_index(op.f("ix_daily_brief_email_deliveries_organization_id"), table_name="daily_brief_email_deliveries")
    op.drop_index(op.f("ix_daily_brief_email_deliveries_id"), table_name="daily_brief_email_deliveries")
    op.drop_table("daily_brief_email_deliveries")
    op.drop_column("users", "daily_brief_email_opted_out")
