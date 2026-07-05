"""add opportunity history and recipient read state

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
"""

from alembic import op
import sqlalchemy as sa


revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "opportunity_history_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("event_data", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "id",
        "organization_id",
        "opportunity_id",
        "event_type",
        "source",
        "occurred_at",
    ):
        op.create_index(
            op.f(f"ix_opportunity_history_events_{column}"),
            "opportunity_history_events",
            [column],
            unique=False,
        )

    op.create_table(
        "opportunity_history_recipients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("history_event_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["history_event_id"],
            ["opportunity_history_events.id"],
        ),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "history_event_id",
            "user_id",
            name="uq_opportunity_history_recipient_event_user",
        ),
    )
    for column in (
        "id",
        "organization_id",
        "opportunity_id",
        "history_event_id",
        "user_id",
        "read_at",
    ):
        op.create_index(
            op.f(f"ix_opportunity_history_recipients_{column}"),
            "opportunity_history_recipients",
            [column],
            unique=False,
        )

    op.execute(
        """
        INSERT INTO opportunity_history_events
            (organization_id, opportunity_id, event_type, source, occurred_at, event_data)
        SELECT
            organization_id,
            id,
            'opportunity_imported',
            source,
            COALESCE(created_at, upserted_at, CURRENT_TIMESTAMP),
            NULL
        FROM opportunities
        """
    )


def downgrade():
    op.drop_table("opportunity_history_recipients")
    op.drop_table("opportunity_history_events")
