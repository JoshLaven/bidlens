"""add source update audit fields

Revision ID: 9f0a1b2c3d4e
Revises: 8e9f0a1b2c3d
"""

from alembic import op
import sqlalchemy as sa


revision = "9f0a1b2c3d4e"
down_revision = "8e9f0a1b2c3d"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("opportunity_update_events") as batch_op:
        batch_op.add_column(
            sa.Column("ingestion_run_id", sa.Integer(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("salesforce_response", sa.JSON(), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_opportunity_update_events_ingestion_run_id",
            "ingestion_runs",
            ["ingestion_run_id"],
            ["id"],
        )
    op.create_index(
        op.f("ix_opportunity_update_events_ingestion_run_id"),
        "opportunity_update_events",
        ["ingestion_run_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_opportunity_update_events_ingestion_run_id"),
        table_name="opportunity_update_events",
    )
    with op.batch_alter_table("opportunity_update_events") as batch_op:
        batch_op.drop_constraint(
            "fk_opportunity_update_events_ingestion_run_id",
            type_="foreignkey",
        )
        batch_op.drop_column("salesforce_response")
        batch_op.drop_column("ingestion_run_id")
