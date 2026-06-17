"""source agnostic opportunity ids

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-16 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    if not _table_exists("opportunities"):
        return

    if not _has_column("opportunities", "source"):
        op.add_column("opportunities", sa.Column("source", sa.String(), nullable=True, server_default="sam"))
    if not _has_column("opportunities", "source_record_id"):
        op.add_column("opportunities", sa.Column("source_record_id", sa.String(), nullable=True))
    if not _has_column("opportunities", "solicitation_number"):
        op.add_column("opportunities", sa.Column("solicitation_number", sa.String(), nullable=True))
    if not _has_column("opportunities", "source_url"):
        op.add_column("opportunities", sa.Column("source_url", sa.String(), nullable=True))
    if not _has_column("opportunities", "raw_source_payload"):
        op.add_column("opportunities", sa.Column("raw_source_payload", sa.JSON(), nullable=True))
    if not _has_column("opportunities", "govwin_staging_id"):
        op.add_column("opportunities", sa.Column("govwin_staging_id", sa.String(), nullable=True))

    op.execute(sa.text("UPDATE opportunities SET source = COALESCE(NULLIF(source, ''), 'sam')"))
    op.execute(
        sa.text(
            """
            UPDATE opportunities
            SET source_record_id = COALESCE(NULLIF(source_record_id, ''), sam_notice_id, 'legacy-' || id)
            WHERE source_record_id IS NULL OR source_record_id = ''
            """
        )
    )
    op.execute(sa.text("UPDATE opportunities SET sam_notice_id = source_record_id WHERE sam_notice_id IS NULL AND source = 'sam'"))
    op.execute(sa.text("UPDATE opportunities SET source_url = sam_url WHERE source_url IS NULL AND sam_url IS NOT NULL"))
    op.execute(sa.text("UPDATE opportunities SET sam_url = source_url WHERE sam_url IS NULL AND source = 'sam' AND source_url IS NOT NULL"))

    indexes = _index_names("opportunities")
    if "uq_opportunity_org_sam_notice" in indexes:
        op.drop_index("uq_opportunity_org_sam_notice", table_name="opportunities")

    indexes = _index_names("opportunities")
    if "ix_opportunities_source" not in indexes:
        op.create_index("ix_opportunities_source", "opportunities", ["source"], unique=False)
    if "ix_opportunities_source_record_id" not in indexes:
        op.create_index("ix_opportunities_source_record_id", "opportunities", ["source_record_id"], unique=False)
    if "ix_opportunities_solicitation_number" not in indexes:
        op.create_index("ix_opportunities_solicitation_number", "opportunities", ["solicitation_number"], unique=False)
    if "ix_opportunities_govwin_staging_id" not in indexes:
        op.create_index("ix_opportunities_govwin_staging_id", "opportunities", ["govwin_staging_id"], unique=False)
    if "uq_opportunity_org_source_record" not in indexes:
        op.create_index(
            "uq_opportunity_org_source_record",
            "opportunities",
            ["organization_id", "source", "source_record_id"],
            unique=True,
        )


def downgrade() -> None:
    indexes = _index_names("opportunities")
    if "uq_opportunity_org_source_record" in indexes:
        op.drop_index("uq_opportunity_org_source_record", table_name="opportunities")
    if "ix_opportunities_govwin_staging_id" in indexes:
        op.drop_index("ix_opportunities_govwin_staging_id", table_name="opportunities")
    if "ix_opportunities_solicitation_number" in indexes:
        op.drop_index("ix_opportunities_solicitation_number", table_name="opportunities")
    if "ix_opportunities_source_record_id" in indexes:
        op.drop_index("ix_opportunities_source_record_id", table_name="opportunities")
    if "ix_opportunities_source" in indexes:
        op.drop_index("ix_opportunities_source", table_name="opportunities")
    if "uq_opportunity_org_sam_notice" not in _index_names("opportunities"):
        op.create_index("uq_opportunity_org_sam_notice", "opportunities", ["organization_id", "sam_notice_id"], unique=True)
