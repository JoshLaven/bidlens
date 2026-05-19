"""add opportunity description fields

Revision ID: 4ad1c7cb8c3d
Revises: bb8491f36706
Create Date: 2026-04-28 12:15:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "4ad1c7cb8c3d"
down_revision = "bb8491f36706"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("opportunities") as batch_op:
        batch_op.add_column(sa.Column("description_url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("description_text", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("opportunities") as batch_op:
        batch_op.drop_column("description_text")
        batch_op.drop_column("description_url")
