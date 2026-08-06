"""add organization resources

Revision ID: 8a9b0c1d2e3f
Revises: 7f8a9b0c1d2e
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa


revision = "8a9b0c1d2e3f"
down_revision = "7f8a9b0c1d2e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_resources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("link_url", sa.Text(), nullable=True),
        sa.Column("note_content", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_organization_resources_id"), "organization_resources", ["id"], unique=False)
    op.create_index(
        op.f("ix_organization_resources_organization_id"),
        "organization_resources",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_organization_resources_org_position",
        "organization_resources",
        ["organization_id", "position", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_organization_resources_org_position", table_name="organization_resources")
    op.drop_index(op.f("ix_organization_resources_organization_id"), table_name="organization_resources")
    op.drop_index(op.f("ix_organization_resources_id"), table_name="organization_resources")
    op.drop_table("organization_resources")
