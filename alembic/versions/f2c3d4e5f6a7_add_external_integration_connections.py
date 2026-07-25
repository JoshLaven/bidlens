"""add external integration connections

Revision ID: f2c3d4e5f6a7
Revises: f1b2c3d4e5f6
Create Date: 2026-07-23
"""

from alembic import op
import sqlalchemy as sa


revision = "f2c3d4e5f6a7"
down_revision = "f1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_integration_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("connection_status", sa.String(), server_default="connected", nullable=False),
        sa.Column("external_tenant_id", sa.String(), nullable=True),
        sa.Column("external_user_id", sa.String(), nullable=True),
        sa.Column("connected_email", sa.String(), nullable=True),
        sa.Column("connected_display_name", sa.String(), nullable=True),
        sa.Column("encrypted_access_token", sa.Text(), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("granted_scopes", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id", "provider", name="uq_external_connection_workspace_user_provider"),
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "external_tenant_id",
            "external_user_id",
            name="uq_external_connection_workspace_provider_identity",
        ),
    )
    for column in (
        "id",
        "workspace_id",
        "user_id",
        "provider",
        "connection_status",
        "external_tenant_id",
        "external_user_id",
    ):
        op.create_index(op.f(f"ix_external_integration_connections_{column}"), "external_integration_connections", [column], unique=False)
    op.create_index(
        "ix_external_connection_workspace_provider_status",
        "external_integration_connections",
        ["workspace_id", "provider", "connection_status"],
        unique=False,
    )

    op.create_table(
        "external_integration_oauth_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("state_digest", sa.String(), nullable=False),
        sa.Column("encrypted_code_verifier", sa.Text(), nullable=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("return_path", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_digest", name="uq_external_oauth_state_digest"),
    )
    for column in ("id", "provider", "state_digest", "workspace_id", "user_id", "expires_at"):
        op.create_index(op.f(f"ix_external_integration_oauth_states_{column}"), "external_integration_oauth_states", [column], unique=False)
    op.create_index(
        "ix_external_oauth_state_provider_user_workspace",
        "external_integration_oauth_states",
        ["provider", "user_id", "workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_external_oauth_state_provider_user_workspace", table_name="external_integration_oauth_states")
    for column in ("expires_at", "user_id", "workspace_id", "state_digest", "provider", "id"):
        op.drop_index(op.f(f"ix_external_integration_oauth_states_{column}"), table_name="external_integration_oauth_states")
    op.drop_table("external_integration_oauth_states")

    op.drop_index("ix_external_connection_workspace_provider_status", table_name="external_integration_connections")
    for column in ("external_user_id", "external_tenant_id", "connection_status", "provider", "user_id", "workspace_id", "id"):
        op.drop_index(op.f(f"ix_external_integration_connections_{column}"), table_name="external_integration_connections")
    op.drop_table("external_integration_connections")
