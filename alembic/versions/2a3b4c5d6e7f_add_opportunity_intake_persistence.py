"""add opportunity intake draft and source material persistence

Revision ID: 2a3b4c5d6e7f
Revises: 1a2b3c4d5e6f
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa


revision = "2a3b4c5d6e7f"
down_revision = "1a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunity_intake_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("intake_method", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="DRAFT", nullable=False),
        sa.Column("candidate_fields_json", sa.JSON(), nullable=False),
        sa.Column("extraction_metadata_json", sa.JSON(), nullable=True),
        sa.Column("validation_errors_json", sa.JSON(), nullable=True),
        sa.Column("add_to_shortlist", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("published_opportunity_id", sa.Integer(), nullable=True),
        sa.Column("publish_idempotency_key", sa.String(), nullable=True),
        sa.Column("internal_reference", sa.String(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["published_opportunity_id"], ["opportunities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("internal_reference", name="uq_opportunity_intake_internal_reference"),
        sa.UniqueConstraint("publish_idempotency_key", name="uq_opportunity_intake_publish_idempotency"),
    )
    for column in (
        "id", "organization_id", "workspace_id", "created_by_user_id", "intake_method",
        "status", "published_opportunity_id", "expires_at",
    ):
        op.create_index(op.f(f"ix_opportunity_intake_drafts_{column}"), "opportunity_intake_drafts", [column], unique=False)
    op.create_index(
        "ix_opportunity_intake_drafts_org_workspace_status",
        "opportunity_intake_drafts",
        ["organization_id", "workspace_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_opportunity_intake_drafts_workspace_creator",
        "opportunity_intake_drafts",
        ["workspace_id", "created_by_user_id"],
        unique=False,
    )

    op.create_table(
        "opportunity_source_materials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("intake_draft_id", sa.Integer(), nullable=True),
        sa.Column("opportunity_id", sa.Integer(), nullable=True),
        sa.Column("parent_material_id", sa.Integer(), nullable=True),
        sa.Column("material_type", sa.String(), nullable=False),
        sa.Column("original_filename", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256_digest", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("provider_metadata_json", sa.JSON(), nullable=True),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("internet_message_id", sa.String(), nullable=True),
        sa.Column("parsed_metadata_json", sa.JSON(), nullable=True),
        sa.Column("parse_status", sa.String(), server_default="PENDING", nullable=False),
        sa.Column("parse_error_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["intake_draft_id"], ["opportunity_intake_drafts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["parent_material_id"], ["opportunity_source_materials.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_opportunity_source_material_storage_key"),
    )
    for column in (
        "id", "organization_id", "workspace_id", "intake_draft_id", "opportunity_id",
        "parent_material_id", "material_type", "sha256_digest", "provider_message_id",
        "internet_message_id", "parse_status",
    ):
        op.create_index(op.f(f"ix_opportunity_source_materials_{column}"), "opportunity_source_materials", [column], unique=False)
    op.create_index("ix_opportunity_source_materials_workspace_draft", "opportunity_source_materials", ["workspace_id", "intake_draft_id"], unique=False)
    op.create_index("ix_opportunity_source_materials_workspace_opportunity", "opportunity_source_materials", ["workspace_id", "opportunity_id"], unique=False)
    op.create_index("ix_opportunity_source_materials_workspace_sha256", "opportunity_source_materials", ["workspace_id", "sha256_digest"], unique=False)
    op.create_index("ix_opportunity_source_materials_workspace_provider_message", "opportunity_source_materials", ["workspace_id", "provider_message_id"], unique=False)
    op.create_index("ix_opportunity_source_materials_workspace_internet_message", "opportunity_source_materials", ["workspace_id", "internet_message_id"], unique=False)


def downgrade() -> None:
    op.drop_table("opportunity_source_materials")
    op.drop_table("opportunity_intake_drafts")
