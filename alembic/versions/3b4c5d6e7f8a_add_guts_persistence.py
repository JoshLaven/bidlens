"""add Get Up to Speed append-only persistence

Revision ID: 3b4c5d6e7f8a
Revises: 2a3b4c5d6e7f
Create Date: 2026-07-31
"""

from alembic import op
import sqlalchemy as sa


revision = "3b4c5d6e7f8a"
down_revision = "2a3b4c5d6e7f"
branch_labels = None
depends_on = None


GENERATION_TABLE = "opportunity_knowledge_brief_generations"
STATEMENT_TABLE = "opportunity_knowledge_brief_statements"
SOURCE_TABLE = "opportunity_knowledge_brief_sources"
LINK_TABLE = "opportunity_knowledge_brief_statement_sources"


def upgrade() -> None:
    op.create_table(
        GENERATION_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("generated_by_user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("manifest_version", sa.String(), nullable=False),
        sa.Column("output_schema_version", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("source_snapshot_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_snapshot_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_source_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_state_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("source_summary_json", sa.JSON(), nullable=True),
        sa.Column("warning_metadata_json", sa.JSON(), nullable=True),
        sa.Column("statistics_json", sa.JSON(), nullable=True),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("input_character_count", sa.Integer(), nullable=True),
        sa.Column("estimated_input_tokens", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("validation_retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("authorization_ms", sa.Integer(), nullable=True),
        sa.Column("current_state_ms", sa.Integer(), nullable=True),
        sa.Column("official_evidence_ms", sa.Integer(), nullable=True),
        sa.Column("communication_ms", sa.Integer(), nullable=True),
        sa.Column("notes_ms", sa.Integer(), nullable=True),
        sa.Column("history_ms", sa.Integer(), nullable=True),
        sa.Column("manifest_ms", sa.Integer(), nullable=True),
        sa.Column("model_ms", sa.Integer(), nullable=True),
        sa.Column("validation_ms", sa.Integer(), nullable=True),
        sa.Column("persistence_ms", sa.Integer(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("reproducibility_status", sa.String(), server_default="not_reproducible", nullable=False),
        sa.Column("degraded_source_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("failure_category", sa.String(), nullable=True),
        sa.Column("failure_stage", sa.String(), nullable=True),
        sa.Column("safe_error_message", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["generated_by_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("id", "organization_id", "workspace_id", "opportunity_id", "generated_by_user_id", "status"):
        op.create_index(op.f(f"ix_{GENERATION_TABLE}_{column}"), GENERATION_TABLE, [column], unique=False)
    op.create_index("ix_guts_generation_org_opp_status_completed", GENERATION_TABLE, ["organization_id", "opportunity_id", "status", "completed_at"])
    op.create_index("ix_guts_generation_opp_status_requested", GENERATION_TABLE, ["opportunity_id", "status", "requested_at"])
    op.create_index("ix_guts_generation_manifest_hash", GENERATION_TABLE, ["manifest_hash"])
    op.create_index("ix_guts_generation_user_requested", GENERATION_TABLE, ["generated_by_user_id", "requested_at"])
    op.create_index(
        "uq_guts_generation_active_org_opp",
        GENERATION_TABLE,
        ["organization_id", "opportunity_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
        sqlite_where=sa.text("status IN ('pending', 'running')"),
    )

    op.create_table(
        STATEMENT_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("generation_id", sa.Integer(), nullable=False),
        sa.Column("statement_key", sa.String(), nullable=False),
        sa.Column("placement_type", sa.String(), nullable=False),
        sa.Column("section_type", sa.String(), nullable=True),
        sa.Column("section_title", sa.String(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("importance", sa.String(), nullable=False),
        sa.Column("confidence", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["generation_id"], [f"{GENERATION_TABLE}.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_id", "statement_key", name="uq_guts_statement_generation_key"),
    )
    op.create_index(op.f(f"ix_{STATEMENT_TABLE}_id"), STATEMENT_TABLE, ["id"])
    op.create_index(op.f(f"ix_{STATEMENT_TABLE}_generation_id"), STATEMENT_TABLE, ["generation_id"])
    op.create_index("ix_guts_statement_generation_placement_section_position", STATEMENT_TABLE, ["generation_id", "placement_type", "section_type", "position"])

    op.create_table(
        SOURCE_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("generation_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("source_class", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("authority", sa.String(), nullable=False),
        sa.Column("verification", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("citation_label", sa.String(), nullable=False),
        sa.Column("author_display_name", sa.String(), nullable=True),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("author_address", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at_source", sa.DateTime(timezone=True), nullable=True),
        sa.Column("internal_model_name", sa.String(), nullable=True),
        sa.Column("internal_record_id", sa.String(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("parser_name", sa.String(), nullable=True),
        sa.Column("parser_version", sa.String(), nullable=True),
        sa.Column("retained_by_bidlens", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("selected_character_count", sa.Integer(), nullable=True),
        sa.Column("original_character_count", sa.Integer(), nullable=True),
        sa.Column("was_truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["generation_id"], [f"{GENERATION_TABLE}.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_id", "source_id", name="uq_guts_source_generation_source_id"),
    )
    for column in ("id", "generation_id", "author_user_id"):
        op.create_index(op.f(f"ix_{SOURCE_TABLE}_{column}"), SOURCE_TABLE, [column])
    op.create_index("ix_guts_source_generation_class", SOURCE_TABLE, ["generation_id", "source_class"])
    op.create_index("ix_guts_source_internal_record", SOURCE_TABLE, ["internal_model_name", "internal_record_id"])
    op.create_index("ix_guts_source_content_hash", SOURCE_TABLE, ["content_hash"])

    op.create_table(
        LINK_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("statement_id", sa.Integer(), nullable=False),
        sa.Column("brief_source_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["brief_source_id"], [f"{SOURCE_TABLE}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["statement_id"], [f"{STATEMENT_TABLE}.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("statement_id", "brief_source_id", name="uq_guts_statement_source_link"),
    )
    op.create_index(op.f(f"ix_{LINK_TABLE}_id"), LINK_TABLE, ["id"])
    op.create_index(op.f(f"ix_{LINK_TABLE}_statement_id"), LINK_TABLE, ["statement_id"])
    op.create_index("ix_guts_statement_source_brief_source", LINK_TABLE, ["brief_source_id"])


def downgrade() -> None:
    op.drop_table(LINK_TABLE)
    op.drop_table(SOURCE_TABLE)
    op.drop_table(STATEMENT_TABLE)
    op.drop_table(GENERATION_TABLE)
