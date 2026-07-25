"""add opportunity email send lifecycle

Revision ID: f3d4e5f6a7b8
Revises: f2c3d4e5f6a7
Create Date: 2026-07-23
"""

from alembic import op
import sqlalchemy as sa


revision = "f3d4e5f6a7b8"
down_revision = "f2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("send_status", sa.String(), nullable=True),
        sa.Column("send_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_for_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uncertain_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("send_error_code", sa.String(), nullable=True),
        sa.Column("idempotency_key_digest", sa.String(), nullable=True),
        sa.Column("idempotency_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recipient_count", sa.Integer(), nullable=True),
    ):
        op.add_column("opportunity_conversations", column)
    op.create_index(op.f("ix_opportunity_conversations_send_status"), "opportunity_conversations", ["send_status"], unique=False)
    op.create_index(op.f("ix_opportunity_conversations_idempotency_key_digest"), "opportunity_conversations", ["idempotency_key_digest"], unique=True)
    op.create_index(op.f("ix_opportunity_conversations_idempotency_expires_at"), "opportunity_conversations", ["idempotency_expires_at"], unique=False)

    op.create_table(
        "opportunity_conversation_send_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key_digest", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("recipient_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["opportunity_conversations.id"]),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key_digest", name="uq_opp_conversation_send_attempt_idempotency"),
    )
    for column in ("id", "workspace_id", "opportunity_id", "user_id", "conversation_id", "idempotency_key_digest", "status", "expires_at"):
        op.create_index(op.f(f"ix_opportunity_conversation_send_attempts_{column}"), "opportunity_conversation_send_attempts", [column], unique=False)
    op.create_index(
        "ix_opp_conversation_send_attempt_scope",
        "opportunity_conversation_send_attempts",
        ["workspace_id", "opportunity_id", "user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_opp_conversation_send_attempt_scope", table_name="opportunity_conversation_send_attempts")
    for column in ("expires_at", "status", "idempotency_key_digest", "conversation_id", "user_id", "opportunity_id", "workspace_id", "id"):
        op.drop_index(op.f(f"ix_opportunity_conversation_send_attempts_{column}"), table_name="opportunity_conversation_send_attempts")
    op.drop_table("opportunity_conversation_send_attempts")

    op.drop_index(op.f("ix_opportunity_conversations_idempotency_expires_at"), table_name="opportunity_conversations")
    op.drop_index(op.f("ix_opportunity_conversations_idempotency_key_digest"), table_name="opportunity_conversations")
    op.drop_index(op.f("ix_opportunity_conversations_send_status"), table_name="opportunity_conversations")
    for column in (
        "recipient_count",
        "idempotency_expires_at",
        "idempotency_key_digest",
        "send_error_code",
        "uncertain_at",
        "failed_at",
        "accepted_for_delivery_at",
        "send_requested_at",
        "send_status",
    ):
        op.drop_column("opportunity_conversations", column)
