"""add opportunity monitor

Revision ID: 7c8d9e0f1a2b
Revises: 6b7c8d9e0f1a
"""

from alembic import op
import sqlalchemy as sa


revision = "7c8d9e0f1a2b"
down_revision = "6b7c8d9e0f1a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "opportunities",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE opportunities "
        "SET last_seen_at = COALESCE(upserted_at, updated_at, created_at)"
    )

    op.create_table(
        "opportunity_update_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_record_id", sa.String(), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column("salesforce_payload", sa.JSON(), nullable=True),
        sa.Column("salesforce_sync_status", sa.String(), nullable=False),
        sa.Column("salesforce_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("salesforce_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_opportunity_update_events_id"),
        "opportunity_update_events",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_update_events_organization_id"),
        "opportunity_update_events",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_update_events_opportunity_id"),
        "opportunity_update_events",
        ["opportunity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_update_events_source"),
        "opportunity_update_events",
        ["source"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_update_events_source_record_id"),
        "opportunity_update_events",
        ["source_record_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_opportunity_update_events_source_record_id"),
        table_name="opportunity_update_events",
    )
    op.drop_index(
        op.f("ix_opportunity_update_events_source"),
        table_name="opportunity_update_events",
    )
    op.drop_index(
        op.f("ix_opportunity_update_events_opportunity_id"),
        table_name="opportunity_update_events",
    )
    op.drop_index(
        op.f("ix_opportunity_update_events_organization_id"),
        table_name="opportunity_update_events",
    )
    op.drop_index(
        op.f("ix_opportunity_update_events_id"),
        table_name="opportunity_update_events",
    )
    op.drop_table("opportunity_update_events")
    op.drop_column("opportunities", "last_seen_at")
