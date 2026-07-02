"""add sam source configuration

Revision ID: a0b1c2d3e4f5
Revises: 9f0a1b2c3d4e
"""

from alembic import op
import sqlalchemy as sa


revision = "a0b1c2d3e4f5"
down_revision = "9f0a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sam_source_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("naics_codes", sa.JSON(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("agencies", sa.JSON(), nullable=False),
        sa.Column("set_asides", sa.JSON(), nullable=False),
        sa.Column("notice_types", sa.JSON(), nullable=False),
        sa.Column("posted_days_back", sa.Integer(), server_default="30", nullable=False),
        sa.Column("due_days_from", sa.Integer(), nullable=True),
        sa.Column("due_days_to", sa.Integer(), nullable=True),
        sa.Column("active_only", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("max_records", sa.Integer(), server_default="100", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", name="uq_sam_source_config_org"),
    )
    op.create_index(
        op.f("ix_sam_source_configs_organization_id"),
        "sam_source_configs",
        ["organization_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_sam_source_configs_organization_id"),
        table_name="sam_source_configs",
    )
    op.drop_table("sam_source_configs")
