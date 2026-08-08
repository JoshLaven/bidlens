"""add Microsoft workspace tenant metadata

Revision ID: d4e5f6a7b9c0
Revises: c3d4e5f6a7b9
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b9c0"
down_revision = "c3d4e5f6a7b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workspace_integrations") as batch_op:
        batch_op.add_column(sa.Column("external_tenant_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("tenant_display_name", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workspace_integrations") as batch_op:
        batch_op.drop_column("tenant_display_name")
        batch_op.drop_column("external_tenant_id")
