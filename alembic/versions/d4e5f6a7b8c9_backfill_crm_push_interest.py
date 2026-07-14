"""backfill crm push interest

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-16 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "opportunities" not in tables or "votes" not in tables:
        return
    if not _has_column("opportunities", "crm_pushed_by"):
        return

    op.execute(
        sa.text(
            """
            INSERT INTO votes (org_id, opp_id, user_id, vote, updated_at)
            SELECT o.organization_id, o.id, o.crm_pushed_by, 'PURSUE', CURRENT_TIMESTAMP
            FROM opportunities o
            WHERE o.crm_pushed IS TRUE
              AND o.crm_pushed_by IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM votes v
                  WHERE v.org_id = o.organization_id
                    AND v.opp_id = o.id
                    AND v.user_id = o.crm_pushed_by
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE votes
            SET vote = 'PURSUE', updated_at = CURRENT_TIMESTAMP
            WHERE EXISTS (
                SELECT 1
                FROM opportunities o
                WHERE o.organization_id = votes.org_id
                  AND o.id = votes.opp_id
                  AND o.crm_pushed IS TRUE
                  AND o.crm_pushed_by = votes.user_id
            )
            """
        )
    )


def downgrade() -> None:
    pass
