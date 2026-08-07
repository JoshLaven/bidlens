"""add opportunity eligibility

Revision ID: a1b2c3d4e5a7
Revises: 9b0c1d2e3f4a
"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5a7"
down_revision = "9b0c1d2e3f4a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opportunities", sa.Column("eligibility", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("opportunities", "eligibility")
