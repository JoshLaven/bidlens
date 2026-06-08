"""scope sam notice uniqueness

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-06-07 00:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def _index_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    if table_name not in sa.inspect(bind).get_table_names():
        return set()
    return {idx["name"] for idx in sa.inspect(bind).get_indexes(table_name)}


def upgrade() -> None:
    indexes = _index_names("opportunities")
    if "ix_opportunities_sam_notice_id" in indexes:
        op.drop_index("ix_opportunities_sam_notice_id", table_name="opportunities")
    if "uq_opportunity_org_sam_notice" not in indexes:
        op.create_index(
            "uq_opportunity_org_sam_notice",
            "opportunities",
            ["organization_id", "sam_notice_id"],
            unique=True,
        )
    if "ix_opportunities_sam_notice_id" not in _index_names("opportunities"):
        op.create_index("ix_opportunities_sam_notice_id", "opportunities", ["sam_notice_id"], unique=False)


def downgrade() -> None:
    indexes = _index_names("opportunities")
    if "uq_opportunity_org_sam_notice" in indexes:
        op.drop_index("uq_opportunity_org_sam_notice", table_name="opportunities")
    if "ix_opportunities_sam_notice_id" in _index_names("opportunities"):
        op.drop_index("ix_opportunities_sam_notice_id", table_name="opportunities")
    op.create_index("ix_opportunities_sam_notice_id", "opportunities", ["sam_notice_id"], unique=True)
