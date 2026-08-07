"""add opportunity date shortlisted

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5a7
"""

from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a8"
down_revision = "a1b2c3d4e5a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "opportunities",
        sa.Column("date_shortlisted", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunities", "date_shortlisted")
