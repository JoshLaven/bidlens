"""add GovWin integration settings

Revision ID: 6b7c8d9e0f1a
Revises: 5f6a7b8c9d0e
"""

from alembic import op
import sqlalchemy as sa


revision = "6b7c8d9e0f1a"
down_revision = "5f6a7b8c9d0e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("org_profiles", sa.Column("govwin_credentials_encrypted", sa.Text(), nullable=True))
    op.add_column("org_profiles", sa.Column("govwin_connection_status", sa.String(), nullable=True))
    op.add_column("org_profiles", sa.Column("govwin_last_tested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("org_profiles", sa.Column("govwin_last_sync_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("org_profiles", sa.Column("govwin_last_sync_status", sa.String(), nullable=True))


def downgrade():
    op.drop_column("org_profiles", "govwin_last_sync_status")
    op.drop_column("org_profiles", "govwin_last_sync_at")
    op.drop_column("org_profiles", "govwin_last_tested_at")
    op.drop_column("org_profiles", "govwin_connection_status")
    op.drop_column("org_profiles", "govwin_credentials_encrypted")
