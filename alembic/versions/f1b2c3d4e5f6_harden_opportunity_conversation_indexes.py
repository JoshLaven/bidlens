"""harden opportunity conversation indexes

Revision ID: f1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-07-23
"""

from alembic import op


revision = "f1b2c3d4e5f6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_opportunity_activity_opportunity_occurred",
        "opportunity_activity_events",
        ["opportunity_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_opportunity_activity_conversation_occurred",
        "opportunity_activity_events",
        ["conversation_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_opportunity_activity_conversation_occurred", table_name="opportunity_activity_events")
    op.drop_index("ix_opportunity_activity_opportunity_occurred", table_name="opportunity_activity_events")
