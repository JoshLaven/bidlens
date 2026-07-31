"""add retained source-material extraction cache

Revision ID: 4c5d6e7f8a9b
Revises: 3b4c5d6e7f8a
Create Date: 2026-07-31
"""

from alembic import op
import sqlalchemy as sa


revision = "4c5d6e7f8a9b"
down_revision = "3b4c5d6e7f8a"
branch_labels = None
depends_on = None


TABLE = "opportunity_source_material_extractions"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_material_id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("parser_name", sa.String(), nullable=False),
        sa.Column("parser_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("character_count", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("warnings_json", sa.JSON(), nullable=True),
        sa.Column("failure_category", sa.String(), nullable=True),
        sa.Column("safe_error_message", sa.String(length=500), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_material_id"], ["opportunity_source_materials.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_material_id", "content_hash", "parser_name", "parser_version",
            name="uq_source_material_extraction_cache_key",
        ),
    )
    op.create_index(op.f(f"ix_{TABLE}_id"), TABLE, ["id"])
    op.create_index(op.f(f"ix_{TABLE}_source_material_id"), TABLE, ["source_material_id"])
    op.create_index(op.f(f"ix_{TABLE}_status"), TABLE, ["status"])
    op.create_index("ix_source_material_extraction_material_status", TABLE, ["source_material_id", "status"])
    op.create_index("ix_source_material_extraction_content_hash", TABLE, ["content_hash"])
    op.create_index("ix_source_material_extraction_parser", TABLE, ["parser_name", "parser_version"])


def downgrade() -> None:
    op.drop_table(TABLE)
