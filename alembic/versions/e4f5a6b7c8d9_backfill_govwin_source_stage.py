"""backfill GovWin source stage from raw payload

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
"""

from alembic import op
import sqlalchemy as sa


revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    opportunities = sa.table(
        "opportunities",
        sa.column("id", sa.Integer()),
        sa.column("source", sa.String()),
        sa.column("source_stage", sa.String()),
        sa.column("opportunity_type", sa.String()),
        sa.column("raw_source_payload", sa.JSON()),
    )
    rows = bind.execute(
        sa.select(
            opportunities.c.id,
            opportunities.c.raw_source_payload,
        ).where(
            opportunities.c.source.in_(("govwin_export", "govwin_api")),
            opportunities.c.source_stage.is_(None),
        )
    )
    stage_map = {
        "forecast pre-rfp": "Forecast",
        "pre-rfp": "RFI",
        "post-rfp": "RFP",
    }
    for opportunity_id, raw_payload in rows:
        if not isinstance(raw_payload, dict):
            continue
        source_stage = str(
            raw_payload.get("Status")
            or raw_payload.get("source_stage")
            or raw_payload.get("opportunity_type")
            or ""
        ).strip()
        if not source_stage:
            continue
        values = {"source_stage": source_stage}
        display_stage = stage_map.get(source_stage.casefold())
        if display_stage:
            values["opportunity_type"] = display_stage
        bind.execute(
            opportunities.update()
            .where(opportunities.c.id == opportunity_id)
            .values(**values)
        )


def downgrade():
    # Preserve source-stage fidelity if the schema column remains available.
    pass
