"""backfill govwin sam notice ids

Revision ID: f8c9d0e1f2a3
Revises: f7b8c9d0e1f2
Create Date: 2026-06-16 00:00:00.000000
"""

from alembic import op
import re
import sqlalchemy as sa


revision = "f8c9d0e1f2a3"
down_revision = "f7b8c9d0e1f2"
branch_labels = None
depends_on = None

SAM_OPP_URL_RE = re.compile(r"/opp/([^/?#]+)/", re.IGNORECASE)


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def _extract_sam_notice_id(value: str | None) -> str | None:
    if not value or "sam.gov" not in value.lower():
        return None
    match = SAM_OPP_URL_RE.search(value)
    if not match:
        return None
    notice_id = match.group(1).strip()
    return notice_id or None


def upgrade() -> None:
    if not (
        _has_column("opportunities", "source")
        and _has_column("opportunities", "source_url")
        and _has_column("opportunities", "sam_notice_id")
    ):
        return

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT id, source_url
            FROM opportunities
            WHERE source = 'govwin_export'
              AND source_url IS NOT NULL
              AND (sam_notice_id IS NULL OR sam_notice_id = '')
            """
        )
    ).mappings()
    for row in rows:
        sam_notice_id = _extract_sam_notice_id(row["source_url"])
        if not sam_notice_id:
            continue
        bind.execute(
            sa.text("UPDATE opportunities SET sam_notice_id = :sam_notice_id WHERE id = :id"),
            {"sam_notice_id": sam_notice_id, "id": row["id"]},
        )


def downgrade() -> None:
    # Do not erase extracted identifiers on downgrade; they are useful record metadata.
    pass
