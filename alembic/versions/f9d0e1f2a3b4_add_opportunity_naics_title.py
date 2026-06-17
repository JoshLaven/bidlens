"""add opportunity naics title

Revision ID: f9d0e1f2a3b4
Revises: f8c9d0e1f2a3
Create Date: 2026-06-16 00:00:00.000000
"""

from alembic import op
import json
import sqlalchemy as sa


revision = "f9d0e1f2a3b4"
down_revision = "f8c9d0e1f2a3"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def upgrade() -> None:
    if not _table_exists("opportunities"):
        return

    if not _has_column("opportunities", "naics_title"):
        op.add_column("opportunities", sa.Column("naics_title", sa.String(), nullable=True))

    bind = op.get_bind()
    if not _has_column("opportunities", "raw_source_payload"):
        return

    rows = bind.execute(
        sa.text(
            """
            SELECT id, raw_source_payload
            FROM opportunities
            WHERE source = 'govwin_export'
            """
        )
    ).mappings()
    for row in rows:
        payload = row["raw_source_payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if not isinstance(payload, dict):
            continue
        naics = _clean(payload.get("Primary NAICS Id"))
        naics_title = _clean(payload.get("Primary NAICS Title"))
        bind.execute(
            sa.text(
                """
                UPDATE opportunities
                SET naics = COALESCE(:naics, naics),
                    naics_title = COALESCE(:naics_title, naics_title)
                WHERE id = :id
                """
            ),
            {"id": row["id"], "naics": naics, "naics_title": naics_title},
        )


def downgrade() -> None:
    if _has_column("opportunities", "naics_title"):
        op.drop_column("opportunities", "naics_title")
