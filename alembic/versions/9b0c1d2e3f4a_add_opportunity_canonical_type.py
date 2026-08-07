"""add opportunity canonical type

Revision ID: 9b0c1d2e3f4a
Revises: 8a9b0c1d2e3f
"""

from alembic import op
import sqlalchemy as sa


revision = "9b0c1d2e3f4a"
down_revision = "8a9b0c1d2e3f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opportunities", sa.Column("canonical_type", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("opportunities", "canonical_type")
