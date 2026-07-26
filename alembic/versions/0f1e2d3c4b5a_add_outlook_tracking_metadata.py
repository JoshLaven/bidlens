"""add Outlook tracking metadata

Revision ID: 0f1e2d3c4b5a
Revises: f3d4e5f6a7b8
Create Date: 2026-07-26
"""

from alembic import op
import sqlalchemy as sa


revision = "0f1e2d3c4b5a"
down_revision = "f3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("provider_mailbox_id", sa.String(), nullable=True),
        sa.Column("initial_provider_message_id", sa.String(), nullable=True),
        sa.Column("tracking_status", sa.String(), nullable=True),
        sa.Column("last_successful_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempted_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_provider_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
    ):
        op.add_column("opportunity_conversations", column)
    op.create_index(
        op.f("ix_opportunity_conversations_provider_mailbox_id"),
        "opportunity_conversations",
        ["provider_mailbox_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_conversations_tracking_status"),
        "opportunity_conversations",
        ["tracking_status"],
        unique=False,
    )

    op.create_table(
        "opportunity_communication_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("associated_user_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("provider_mailbox_id", sa.String(), nullable=True),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("provider_conversation_id", sa.String(), nullable=True),
        sa.Column("internet_message_id", sa.String(), nullable=True),
        sa.Column("sender_address", sa.String(), nullable=True),
        sa.Column("sender_display_name", sa.String(), nullable=True),
        sa.Column("recipients_json", sa.JSON(), nullable=True),
        sa.Column("cc_recipients_json", sa.JSON(), nullable=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("body_content_type", sa.String(), nullable=True),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_web_link", sa.Text(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["associated_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["opportunity_conversations.id"]),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "provider_mailbox_id",
            "provider_message_id",
            name="uq_opp_comm_message_workspace_provider_mailbox_message",
        ),
    )
    for column in (
        "id",
        "workspace_id",
        "opportunity_id",
        "conversation_id",
        "associated_user_id",
        "provider",
        "direction",
        "provider_mailbox_id",
        "provider_message_id",
        "provider_conversation_id",
        "internet_message_id",
        "provider_timestamp",
    ):
        op.create_index(
            op.f(f"ix_opportunity_communication_messages_{column}"),
            "opportunity_communication_messages",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_opp_comm_message_workspace_opportunity_timestamp",
        "opportunity_communication_messages",
        ["workspace_id", "opportunity_id", "provider_timestamp"],
        unique=False,
    )
    op.create_index(
        "ix_opp_comm_message_workspace_provider_conversation",
        "opportunity_communication_messages",
        ["workspace_id", "provider", "provider_conversation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_opp_comm_message_workspace_provider_conversation",
        table_name="opportunity_communication_messages",
    )
    op.drop_index(
        "ix_opp_comm_message_workspace_opportunity_timestamp",
        table_name="opportunity_communication_messages",
    )
    for column in reversed((
        "id",
        "workspace_id",
        "opportunity_id",
        "conversation_id",
        "associated_user_id",
        "provider",
        "direction",
        "provider_mailbox_id",
        "provider_message_id",
        "provider_conversation_id",
        "internet_message_id",
        "provider_timestamp",
    )):
        op.drop_index(
            op.f(f"ix_opportunity_communication_messages_{column}"),
            table_name="opportunity_communication_messages",
        )
    op.drop_table("opportunity_communication_messages")

    op.drop_index(op.f("ix_opportunity_conversations_tracking_status"), table_name="opportunity_conversations")
    op.drop_index(op.f("ix_opportunity_conversations_provider_mailbox_id"), table_name="opportunity_conversations")
    for column in reversed((
        "provider_mailbox_id",
        "initial_provider_message_id",
        "tracking_status",
        "last_successful_sync_at",
        "last_attempted_sync_at",
        "last_provider_message_at",
        "last_sync_error",
    )):
        op.drop_column("opportunity_conversations", column)
