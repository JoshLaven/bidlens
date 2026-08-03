"""add canonical Team Summary freshness metadata

Revision ID: 6e7f8a9b0c1d
Revises: 5d6e7f8a9b0c
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "6e7f8a9b0c1d"
down_revision = "5d6e7f8a9b0c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opportunity_communication_summaries", sa.Column("note_count_included", sa.Integer(), server_default="0", nullable=False))
    op.add_column("opportunity_communication_summaries", sa.Column("note_count_available", sa.Integer(), server_default="0", nullable=False))
    op.add_column("opportunity_communication_summaries", sa.Column("latest_note_timestamp_included", sa.DateTime(timezone=True), nullable=True))
    op.add_column("opportunity_communication_summaries", sa.Column("evidence_fingerprint", sa.String(length=64), nullable=True))
    op.add_column("opportunity_communication_summaries", sa.Column("input_contract_version", sa.String(), nullable=True))
    op.add_column("opportunity_communication_summaries", sa.Column("prompt_version", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("opportunity_communication_summaries", "prompt_version")
    op.drop_column("opportunity_communication_summaries", "input_contract_version")
    op.drop_column("opportunity_communication_summaries", "evidence_fingerprint")
    op.drop_column("opportunity_communication_summaries", "latest_note_timestamp_included")
    op.drop_column("opportunity_communication_summaries", "note_count_available")
    op.drop_column("opportunity_communication_summaries", "note_count_included")
