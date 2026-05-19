"""add opportunity notes

Revision ID: 5b3e0cf2f8a1
Revises: 4ad1c7cb8c3d
Create Date: 2026-04-28 18:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "5b3e0cf2f8a1"
down_revision = "4ad1c7cb8c3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunity_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_opportunity_notes_id"), "opportunity_notes", ["id"], unique=False)
    op.create_index(op.f("ix_opportunity_notes_org_id"), "opportunity_notes", ["org_id"], unique=False)
    op.create_index(op.f("ix_opportunity_notes_opportunity_id"), "opportunity_notes", ["opportunity_id"], unique=False)
    op.create_index(op.f("ix_opportunity_notes_user_id"), "opportunity_notes", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_opportunity_notes_user_id"), table_name="opportunity_notes")
    op.drop_index(op.f("ix_opportunity_notes_opportunity_id"), table_name="opportunity_notes")
    op.drop_index(op.f("ix_opportunity_notes_org_id"), table_name="opportunity_notes")
    op.drop_index(op.f("ix_opportunity_notes_id"), table_name="opportunity_notes")
    op.drop_table("opportunity_notes")
