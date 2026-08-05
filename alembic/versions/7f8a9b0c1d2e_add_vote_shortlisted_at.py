"""add canonical shortlist transition timestamp

Revision ID: 7f8a9b0c1d2e
Revises: 6e7f8a9b0c1d
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa


revision = "7f8a9b0c1d2e"
down_revision = "6e7f8a9b0c1d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("votes", sa.Column("shortlisted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_votes_shortlisted_at", "votes", ["shortlisted_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_votes_shortlisted_at", table_name="votes")
    op.drop_column("votes", "shortlisted_at")
