"""add workspace-scoped Salesforce connections

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
"""

from alembic import op
import sqlalchemy as sa


revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "salesforce_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("instance_url", sa.String(), nullable=True),
        sa.Column("salesforce_org_id", sa.String(), nullable=True),
        sa.Column("connected_user_id", sa.String(), nullable=True),
        sa.Column("connected_username", sa.String(), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("encrypted_access_token", sa.Text(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), server_default="not_connected", nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_connection_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", name="uq_salesforce_connection_workspace"),
    )
    op.create_index("ix_salesforce_connections_workspace_id", "salesforce_connections", ["workspace_id"])
    op.create_table(
        "salesforce_oauth_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state_digest", sa.String(), nullable=False),
        sa.Column("encrypted_code_verifier", sa.Text(), nullable=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("return_path", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("state_digest", name="uq_salesforce_oauth_state_digest"),
    )
    op.create_index("ix_salesforce_oauth_states_state_digest", "salesforce_oauth_states", ["state_digest"])
    op.create_index("ix_salesforce_oauth_states_workspace_id", "salesforce_oauth_states", ["workspace_id"])
    op.create_index("ix_salesforce_oauth_states_user_id", "salesforce_oauth_states", ["user_id"])
    op.create_index("ix_salesforce_oauth_states_expires_at", "salesforce_oauth_states", ["expires_at"])


def downgrade() -> None:
    op.drop_table("salesforce_oauth_states")
    op.drop_table("salesforce_connections")
