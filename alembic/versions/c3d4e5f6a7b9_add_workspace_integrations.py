"""add workspace integration foundation

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a8
"""

from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b9"
down_revision = "b2c3d4e5f6a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_integrations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), server_default="individual", nullable=False),
        sa.Column("status", sa.String(), server_default="not_configured", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("provider IN ('microsoft')", name="ck_workspace_integration_provider"),
        sa.CheckConstraint("mode IN ('individual', 'organization')", name="ck_workspace_integration_mode"),
        sa.CheckConstraint(
            "status IN ('not_configured', 'configured', 'action_required', 'disconnected')",
            name="ck_workspace_integration_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "provider", name="uq_workspace_integration_workspace_provider"),
    )
    op.create_index(op.f("ix_workspace_integrations_id"), "workspace_integrations", ["id"], unique=False)
    op.create_index(
        op.f("ix_workspace_integrations_workspace_id"),
        "workspace_integrations",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspace_integrations_provider"),
        "workspace_integrations",
        ["provider"],
        unique=False,
    )
    op.create_index(op.f("ix_workspace_integrations_mode"), "workspace_integrations", ["mode"], unique=False)
    op.create_index(op.f("ix_workspace_integrations_status"), "workspace_integrations", ["status"], unique=False)

    with op.batch_alter_table("external_integration_connections") as batch_op:
        batch_op.add_column(sa.Column("workspace_integration_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            op.f("ix_external_integration_connections_workspace_integration_id"),
            ["workspace_integration_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_external_integration_connection_workspace_integration",
            "workspace_integrations",
            ["workspace_integration_id"],
            ["id"],
        )

    op.execute(
        sa.text(
            """
            INSERT INTO workspace_integrations (workspace_id, provider, mode, status)
            SELECT DISTINCT workspace_id, 'microsoft', 'individual', 'configured'
            FROM external_integration_connections
            WHERE provider = 'microsoft'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE external_integration_connections
            SET workspace_integration_id = (
                SELECT workspace_integrations.id
                FROM workspace_integrations
                WHERE workspace_integrations.workspace_id = external_integration_connections.workspace_id
                  AND workspace_integrations.provider = external_integration_connections.provider
            )
            WHERE provider = 'microsoft'
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("external_integration_connections") as batch_op:
        batch_op.drop_constraint(
            "fk_external_integration_connection_workspace_integration",
            type_="foreignkey",
        )
        batch_op.drop_index(op.f("ix_external_integration_connections_workspace_integration_id"))
        batch_op.drop_column("workspace_integration_id")
    for column in ("status", "mode", "provider", "workspace_id", "id"):
        op.drop_index(op.f(f"ix_workspace_integrations_{column}"), table_name="workspace_integrations")
    op.drop_table("workspace_integrations")
