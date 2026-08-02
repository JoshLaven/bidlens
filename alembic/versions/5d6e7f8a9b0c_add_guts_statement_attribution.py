"""add structured attribution to GUTS statements

Revision ID: 5d6e7f8a9b0c
Revises: 4c5d6e7f8a9b
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "5d6e7f8a9b0c"
down_revision = "4c5d6e7f8a9b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "opportunity_knowledge_brief_statements",
        sa.Column("attribution_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_knowledge_brief_statements", "attribution_json")
