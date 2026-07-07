"""add organization live state

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
"""

from alembic import op
import sqlalchemy as sa


revision = "a6b7c8d9e0f1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "organizations",
        sa.Column(
            "is_live",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute("UPDATE organizations SET is_live = TRUE")


def downgrade():
    op.drop_column("organizations", "is_live")
