"""add opportunity source stage

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
"""

from alembic import op
import sqlalchemy as sa


revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("opportunities", sa.Column("source_stage", sa.String(), nullable=True))
    op.create_index(
        "ix_opportunities_source_stage",
        "opportunities",
        ["source_stage"],
        unique=False,
    )

    # Earlier GovWin imports stored the source stage in opportunity_type.
    op.execute(
        """
        UPDATE opportunities
        SET source_stage = opportunity_type,
            opportunity_type = CASE lower(trim(opportunity_type))
                WHEN 'forecast pre-rfp' THEN 'Forecast'
                WHEN 'pre-rfp' THEN 'RFI'
                WHEN 'post-rfp' THEN 'RFP'
                ELSE opportunity_type
            END
        WHERE source IN ('govwin_export', 'govwin_api')
          AND lower(trim(opportunity_type)) IN (
              'forecast pre-rfp',
              'pre-rfp',
              'post-rfp',
              'source selection'
          )
        """
    )


def downgrade():
    op.drop_index("ix_opportunities_source_stage", table_name="opportunities")
    op.drop_column("opportunities", "source_stage")
