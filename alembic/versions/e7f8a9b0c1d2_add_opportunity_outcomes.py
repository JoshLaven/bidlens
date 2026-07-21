"""add opportunity outcomes

Revision ID: e7f8a9b0c1d2
Revises: e6f7a8b9c0d1
"""

from alembic import op
import sqlalchemy as sa


revision = "e7f8a9b0c1d2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunity_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("opportunities.id"), nullable=False),
        sa.Column("outcome_type", sa.String(), nullable=False),
        sa.Column("recorded_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=True),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "opportunity_id", name="uq_opportunity_outcome_org_opp"),
    )
    op.create_index(op.f("ix_opportunity_outcomes_id"), "opportunity_outcomes", ["id"], unique=False)
    op.create_index(
        op.f("ix_opportunity_outcomes_organization_id"),
        "opportunity_outcomes",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_outcomes_opportunity_id"),
        "opportunity_outcomes",
        ["opportunity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_outcomes_outcome_type"),
        "opportunity_outcomes",
        ["outcome_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_outcomes_recorded_by"),
        "opportunity_outcomes",
        ["recorded_by"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_opportunity_outcomes_recorded_by"), table_name="opportunity_outcomes")
    op.drop_index(op.f("ix_opportunity_outcomes_outcome_type"), table_name="opportunity_outcomes")
    op.drop_index(op.f("ix_opportunity_outcomes_opportunity_id"), table_name="opportunity_outcomes")
    op.drop_index(op.f("ix_opportunity_outcomes_organization_id"), table_name="opportunity_outcomes")
    op.drop_index(op.f("ix_opportunity_outcomes_id"), table_name="opportunity_outcomes")
    op.drop_table("opportunity_outcomes")
