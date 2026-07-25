"""add opportunity conversations

Revision ID: f0a1b2c3d4e5
Revises: e8f9a0b1c2d3
Create Date: 2026-07-23
"""

from alembic import op
import sqlalchemy as sa


revision = "f0a1b2c3d4e5"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunity_conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), server_default="manual", nullable=False),
        sa.Column("external_conversation_id", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("started_by_user_id", sa.Integer(), nullable=True),
        sa.Column("participant_summary", sa.Text(), nullable=True),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["started_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "external_conversation_id",
            name="uq_opportunity_conversation_workspace_provider_external",
        ),
    )
    op.create_index(op.f("ix_opportunity_conversations_id"), "opportunity_conversations", ["id"], unique=False)
    op.create_index(
        op.f("ix_opportunity_conversations_workspace_id"),
        "opportunity_conversations",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_conversations_opportunity_id"),
        "opportunity_conversations",
        ["opportunity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_conversations_provider"),
        "opportunity_conversations",
        ["provider"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_conversations_external_conversation_id"),
        "opportunity_conversations",
        ["external_conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_conversations_started_by_user_id"),
        "opportunity_conversations",
        ["started_by_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_conversations_first_message_at"),
        "opportunity_conversations",
        ["first_message_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_conversations_last_message_at"),
        "opportunity_conversations",
        ["last_message_at"],
        unique=False,
    )
    op.create_index(
        "ix_opportunity_conversations_workspace_opportunity",
        "opportunity_conversations",
        ["workspace_id", "opportunity_id"],
        unique=False,
    )

    op.create_table(
        "opportunity_activity_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["opportunity_conversations.id"]),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_opportunity_activity_events_id"), "opportunity_activity_events", ["id"], unique=False)
    op.create_index(
        op.f("ix_opportunity_activity_events_workspace_id"),
        "opportunity_activity_events",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_activity_events_opportunity_id"),
        "opportunity_activity_events",
        ["opportunity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_activity_events_event_type"),
        "opportunity_activity_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_activity_events_actor_user_id"),
        "opportunity_activity_events",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_activity_events_conversation_id"),
        "opportunity_activity_events",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_activity_events_occurred_at"),
        "opportunity_activity_events",
        ["occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_opportunity_activity_workspace_opportunity_occurred",
        "opportunity_activity_events",
        ["workspace_id", "opportunity_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_opportunity_activity_workspace_opportunity_occurred", table_name="opportunity_activity_events")
    op.drop_index(op.f("ix_opportunity_activity_events_occurred_at"), table_name="opportunity_activity_events")
    op.drop_index(op.f("ix_opportunity_activity_events_conversation_id"), table_name="opportunity_activity_events")
    op.drop_index(op.f("ix_opportunity_activity_events_actor_user_id"), table_name="opportunity_activity_events")
    op.drop_index(op.f("ix_opportunity_activity_events_event_type"), table_name="opportunity_activity_events")
    op.drop_index(op.f("ix_opportunity_activity_events_opportunity_id"), table_name="opportunity_activity_events")
    op.drop_index(op.f("ix_opportunity_activity_events_workspace_id"), table_name="opportunity_activity_events")
    op.drop_index(op.f("ix_opportunity_activity_events_id"), table_name="opportunity_activity_events")
    op.drop_table("opportunity_activity_events")

    op.drop_index("ix_opportunity_conversations_workspace_opportunity", table_name="opportunity_conversations")
    op.drop_index(op.f("ix_opportunity_conversations_last_message_at"), table_name="opportunity_conversations")
    op.drop_index(op.f("ix_opportunity_conversations_first_message_at"), table_name="opportunity_conversations")
    op.drop_index(op.f("ix_opportunity_conversations_started_by_user_id"), table_name="opportunity_conversations")
    op.drop_index(op.f("ix_opportunity_conversations_external_conversation_id"), table_name="opportunity_conversations")
    op.drop_index(op.f("ix_opportunity_conversations_provider"), table_name="opportunity_conversations")
    op.drop_index(op.f("ix_opportunity_conversations_opportunity_id"), table_name="opportunity_conversations")
    op.drop_index(op.f("ix_opportunity_conversations_workspace_id"), table_name="opportunity_conversations")
    op.drop_index(op.f("ix_opportunity_conversations_id"), table_name="opportunity_conversations")
    op.drop_table("opportunity_conversations")
