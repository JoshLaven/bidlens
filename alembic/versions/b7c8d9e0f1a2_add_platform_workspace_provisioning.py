"""add platform workspace provisioning

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
"""

from alembic import op
import sqlalchemy as sa


revision = "b7c8d9e0f1a2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("included_user_count", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_plans_id"), "plans", ["id"], unique=False)
    op.create_index(op.f("ix_plans_code"), "plans", ["code"], unique=True)

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="provisioned"),
        sa.Column("operational_contact_name", sa.String(), nullable=True),
        sa.Column("operational_contact_email", sa.String(), nullable=True),
        sa.Column("billing_contact_name", sa.String(), nullable=True),
        sa.Column("billing_contact_email", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", name="uq_workspace_organization"),
    )
    op.create_index(op.f("ix_workspaces_id"), "workspaces", ["id"], unique=False)
    op.create_index(op.f("ix_workspaces_organization_id"), "workspaces", ["organization_id"], unique=False)
    op.create_index(op.f("ix_workspaces_plan_id"), "workspaces", ["plan_id"], unique=False)
    op.create_index(op.f("ix_workspaces_slug"), "workspaces", ["slug"], unique=True)
    op.create_index(op.f("ix_workspaces_status"), "workspaces", ["status"], unique=False)

    op.create_table(
        "workspace_invitations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False, server_default="admin"),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_workspace_invitation_token"),
    )
    op.create_index(op.f("ix_workspace_invitations_id"), "workspace_invitations", ["id"], unique=False)
    op.create_index(op.f("ix_workspace_invitations_organization_id"), "workspace_invitations", ["organization_id"], unique=False)
    op.create_index(op.f("ix_workspace_invitations_workspace_id"), "workspace_invitations", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_workspace_invitations_email"), "workspace_invitations", ["email"], unique=False)
    op.create_index(op.f("ix_workspace_invitations_token"), "workspace_invitations", ["token"], unique=False)
    op.create_index(op.f("ix_workspace_invitations_status"), "workspace_invitations", ["status"], unique=False)

    op.execute(
        """
        INSERT INTO plans (code, name, included_user_count, created_at)
        VALUES ('professional', 'Professional', 5, CURRENT_TIMESTAMP)
        """
    )
    op.execute(
        """
        INSERT INTO workspaces (
            organization_id,
            plan_id,
            name,
            slug,
            status,
            created_at,
            updated_at
        )
        SELECT
            organizations.id,
            plans.id,
            organizations.name || ' Workspace',
            organizations.slug,
            'provisioned',
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM organizations
        CROSS JOIN plans
        WHERE plans.code = 'professional'
          AND NOT EXISTS (
            SELECT 1
            FROM workspaces
            WHERE workspaces.organization_id = organizations.id
          )
        """
    )


def downgrade():
    op.drop_index(op.f("ix_workspace_invitations_status"), table_name="workspace_invitations")
    op.drop_index(op.f("ix_workspace_invitations_token"), table_name="workspace_invitations")
    op.drop_index(op.f("ix_workspace_invitations_email"), table_name="workspace_invitations")
    op.drop_index(op.f("ix_workspace_invitations_workspace_id"), table_name="workspace_invitations")
    op.drop_index(op.f("ix_workspace_invitations_organization_id"), table_name="workspace_invitations")
    op.drop_index(op.f("ix_workspace_invitations_id"), table_name="workspace_invitations")
    op.drop_table("workspace_invitations")
    op.drop_index(op.f("ix_workspaces_status"), table_name="workspaces")
    op.drop_index(op.f("ix_workspaces_slug"), table_name="workspaces")
    op.drop_index(op.f("ix_workspaces_plan_id"), table_name="workspaces")
    op.drop_index(op.f("ix_workspaces_organization_id"), table_name="workspaces")
    op.drop_index(op.f("ix_workspaces_id"), table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_index(op.f("ix_plans_code"), table_name="plans")
    op.drop_index(op.f("ix_plans_id"), table_name="plans")
    op.drop_table("plans")
