"""add brief source metadata

Revision ID: 8d9f6f57ce21
Revises: 5b3e0cf2f8a1
Create Date: 2026-05-20 21:40:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8d9f6f57ce21"
down_revision = "5b3e0cf2f8a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opportunity_briefs", sa.Column("source_basis", sa.String(), nullable=True))
    op.add_column("opportunity_briefs", sa.Column("sources_used", sa.JSON(), nullable=True))
    op.add_column("opportunity_briefs", sa.Column("filenames_processed", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("opportunity_briefs", "filenames_processed")
    op.drop_column("opportunity_briefs", "sources_used")
    op.drop_column("opportunity_briefs", "source_basis")
