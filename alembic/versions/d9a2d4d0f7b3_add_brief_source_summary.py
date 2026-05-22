"""add brief source summary

Revision ID: d9a2d4d0f7b3
Revises: 8d9f6f57ce21
Create Date: 2026-05-20 22:35:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d9a2d4d0f7b3"
down_revision = "8d9f6f57ce21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opportunity_briefs", sa.Column("source_summary", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("opportunity_briefs", "source_summary")
