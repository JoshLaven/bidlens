"""add brief provider

Revision ID: f2b3641e4d7a
Revises: d9a2d4d0f7b3
Create Date: 2026-05-21 15:05:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f2b3641e4d7a"
down_revision = "d9a2d4d0f7b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opportunity_briefs", sa.Column("provider", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("opportunity_briefs", "provider")
