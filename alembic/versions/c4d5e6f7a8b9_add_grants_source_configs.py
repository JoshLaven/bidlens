"""add Grants.gov source configs

Revision ID: c4d5e6f7a8b9
Revises: c1d2e3f4a5b6
"""

from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7a8b9"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grants_source_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("posted_days_back", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", name="uq_grants_source_config_org"),
    )
    op.create_index(op.f("ix_grants_source_configs_id"), "grants_source_configs", ["id"], unique=False)
    op.create_index(
        op.f("ix_grants_source_configs_organization_id"),
        "grants_source_configs",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_grants_source_configs_enabled"),
        "grants_source_configs",
        ["enabled"],
        unique=False,
    )
    bind = op.get_bind()
    source_filter = (
        "events.payload ->> 'source' = 'grants.gov'"
        if bind.dialect.name == "postgresql"
        else "json_extract(events.payload, '$.source') = 'grants.gov'"
    )
    enabled_literal = "TRUE" if bind.dialect.name == "postgresql" else "1"
    op.execute(
        f"""
        INSERT INTO grants_source_configs (organization_id, enabled, posted_days_back, rows)
        SELECT DISTINCT events.org_id, {enabled_literal}, 7, 25
        FROM events
        WHERE events.event_type = 'opportunity_source_enabled'
          AND {source_filter}
          AND events.org_id IS NOT NULL
        """
    )


def downgrade():
    op.drop_index(op.f("ix_grants_source_configs_enabled"), table_name="grants_source_configs")
    op.drop_index(op.f("ix_grants_source_configs_organization_id"), table_name="grants_source_configs")
    op.drop_index(op.f("ix_grants_source_configs_id"), table_name="grants_source_configs")
    op.drop_table("grants_source_configs")
