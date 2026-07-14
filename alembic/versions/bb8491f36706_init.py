"""init

Revision ID: bb8491f36706
Revises: 
Create Date: 2026-03-03 20:36:12.924806

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'bb8491f36706'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True).with_variant(sa.String(length=36), "sqlite")

    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("plan", sa.String(), nullable=False, server_default="free"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_organizations_id"), "organizations", ["id"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_id"), "users", ["id"], unique=False)
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "opportunities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bidlens_id", uuid_type, nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("agency", sa.String(), nullable=False),
        sa.Column("opportunity_type", sa.String(), nullable=False),
        sa.Column("posted_date", sa.Date(), nullable=False),
        sa.Column("response_deadline", sa.Date(), nullable=False),
        sa.Column("naics", sa.String(), nullable=True),
        sa.Column("set_aside", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sam_url", sa.String(), nullable=False),
        sa.Column("sam_notice_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("upserted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("decision_state", sa.String(), nullable=False, server_default="INBOX"),
        sa.Column("review_stage", sa.String(), nullable=True),
        sa.Column("stage_changed_at", sa.DateTime(), nullable=True),
        sa.Column("stage_changed_by", sa.Integer(), nullable=True),
        sa.Column("archived_reason", sa.String(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("archived_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["archived_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["stage_changed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bidlens_id"),
    )
    op.create_index(op.f("ix_opportunities_id"), "opportunities", ["id"], unique=False)
    op.create_index(op.f("ix_opportunities_bidlens_id"), "opportunities", ["bidlens_id"], unique=True)
    op.create_index(op.f("ix_opportunities_decision_state"), "opportunities", ["decision_state"], unique=False)

    op.create_table(
        "user_opportunities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("internal_deadline", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("watched", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "opportunity_id", name="uq_user_opportunity"),
    )
    op.create_index(op.f("ix_user_opportunities_id"), "user_opportunities", ["id"], unique=False)

    op.create_table(
        "org_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("sam_naics_codes", sa.Text(), nullable=True),
        sa.Column("sam_days_back", sa.Integer(), nullable=True),
        sa.Column("sam_allowed_types", sa.Text(), nullable=True),
        sa.Column("include_keywords", sa.Text(), nullable=True),
        sa.Column("exclude_keywords", sa.Text(), nullable=True),
        sa.Column("include_agencies", sa.Text(), nullable=True),
        sa.Column("exclude_agencies", sa.Text(), nullable=True),
        sa.Column("min_days_out", sa.Integer(), nullable=True),
        sa.Column("max_days_out", sa.Integer(), nullable=True),
        sa.Column("digest_max_items", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("digest_recipients", sa.Text(), nullable=True),
        sa.Column("digest_time_local", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "opportunity_briefs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("brief_json", sa.JSON(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="not_started"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_opportunity_briefs_id"), "opportunity_briefs", ["id"], unique=False)
    op.create_index(op.f("ix_opportunity_briefs_opportunity_id"), "opportunity_briefs", ["opportunity_id"], unique=False)
    op.create_index(op.f("ix_opportunity_briefs_status"), "opportunity_briefs", ["status"], unique=False)

    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="sam.gov"),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inserted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filtered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ingestion_runs_id"), "ingestion_runs", ["id"], unique=False)

    op.create_table(
        "votes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("opp_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("vote", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["opp_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "opp_id", "user_id", name="uq_vote"),
    )
    op.create_index(op.f("ix_votes_org_id"), "votes", ["org_id"], unique=False)
    op.create_index(op.f("ix_votes_opp_id"), "votes", ["opp_id"], unique=False)
    op.create_index(op.f("ix_votes_user_id"), "votes", ["user_id"], unique=False)
    op.create_index(op.f("ix_votes_vote"), "votes", ["vote"], unique=False)

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("opp_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("ui_version", sa.String(), nullable=False, server_default="v1"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["opp_id"], ["opportunities.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_events_id"), "events", ["id"], unique=False)
    op.create_index(op.f("ix_events_ts"), "events", ["ts"], unique=False)
    op.create_index(op.f("ix_events_org_id"), "events", ["org_id"], unique=False)
    op.create_index(op.f("ix_events_user_id"), "events", ["user_id"], unique=False)
    op.create_index(op.f("ix_events_opp_id"), "events", ["opp_id"], unique=False)
    op.create_index(op.f("ix_events_event_type"), "events", ["event_type"], unique=False)

    op.create_table(
        "digest_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("since_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_digest_log_org_id"), "digest_log", ["org_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_digest_log_org_id"), table_name="digest_log")
    op.drop_table("digest_log")
    op.drop_index(op.f("ix_events_event_type"), table_name="events")
    op.drop_index(op.f("ix_events_opp_id"), table_name="events")
    op.drop_index(op.f("ix_events_user_id"), table_name="events")
    op.drop_index(op.f("ix_events_org_id"), table_name="events")
    op.drop_index(op.f("ix_events_ts"), table_name="events")
    op.drop_index(op.f("ix_events_id"), table_name="events")
    op.drop_table("events")
    op.drop_index(op.f("ix_votes_vote"), table_name="votes")
    op.drop_index(op.f("ix_votes_user_id"), table_name="votes")
    op.drop_index(op.f("ix_votes_opp_id"), table_name="votes")
    op.drop_index(op.f("ix_votes_org_id"), table_name="votes")
    op.drop_table("votes")
    op.drop_index(op.f("ix_ingestion_runs_id"), table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
    op.drop_index(op.f("ix_opportunity_briefs_status"), table_name="opportunity_briefs")
    op.drop_index(op.f("ix_opportunity_briefs_opportunity_id"), table_name="opportunity_briefs")
    op.drop_index(op.f("ix_opportunity_briefs_id"), table_name="opportunity_briefs")
    op.drop_table("opportunity_briefs")
    op.drop_table("org_profiles")
    op.drop_index(op.f("ix_user_opportunities_id"), table_name="user_opportunities")
    op.drop_table("user_opportunities")
    op.drop_index(op.f("ix_opportunities_decision_state"), table_name="opportunities")
    op.drop_index(op.f("ix_opportunities_bidlens_id"), table_name="opportunities")
    op.drop_index(op.f("ix_opportunities_id"), table_name="opportunities")
    op.drop_table("opportunities")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_index(op.f("ix_users_id"), table_name="users")
    op.drop_table("users")
    op.drop_index(op.f("ix_organizations_id"), table_name="organizations")
    op.drop_table("organizations")
