"""normalize default workspace backfill

Revision ID: d1e2f3a4b5c6
Revises: c9d8e7f6a5b4
Create Date: 2026-06-07 00:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d1e2f3a4b5c6"
down_revision = "c9d8e7f6a5b4"
branch_labels = None
depends_on = None


def _table_exists(bind, table_name: str) -> bool:
    return table_name in sa.inspect(bind).get_table_names()


def _has_column(bind, table_name: str, column_name: str) -> bool:
    if not _table_exists(bind, table_name):
        return False
    return column_name in {col["name"] for col in sa.inspect(bind).get_columns(table_name)}


def _exec_if_table(bind, table_name: str, sql: str, **params) -> None:
    if _table_exists(bind, table_name):
        op.execute(sa.text(sql).bindparams(**params))


def upgrade() -> None:
    bind = op.get_bind()
    default_org_id = bind.execute(
        sa.text("SELECT id FROM organizations WHERE slug = 'default-workspace' ORDER BY id ASC LIMIT 1")
    ).scalar()
    if not default_org_id:
        return

    if _has_column(bind, "opportunities", "organization_id"):
        _exec_if_table(bind, "opportunities", "UPDATE opportunities SET organization_id = :org_id", org_id=default_org_id)
    if _has_column(bind, "opportunity_briefs", "organization_id"):
        _exec_if_table(bind, "opportunity_briefs", "UPDATE opportunity_briefs SET organization_id = :org_id", org_id=default_org_id)
    if _has_column(bind, "company_profiles", "org_id"):
        _exec_if_table(bind, "company_profiles", "UPDATE company_profiles SET org_id = :org_id", org_id=default_org_id)
    if _has_column(bind, "opportunity_notes", "org_id"):
        _exec_if_table(bind, "opportunity_notes", "UPDATE opportunity_notes SET org_id = :org_id", org_id=default_org_id)
    if _has_column(bind, "user_opportunities", "organization_id"):
        _exec_if_table(bind, "user_opportunities", "UPDATE user_opportunities SET organization_id = :org_id", org_id=default_org_id)
    if _has_column(bind, "votes", "org_id"):
        _exec_if_table(bind, "votes", "UPDATE votes SET org_id = :org_id", org_id=default_org_id)
    if _has_column(bind, "events", "org_id"):
        _exec_if_table(bind, "events", "UPDATE events SET org_id = :org_id WHERE org_id IS NOT NULL", org_id=default_org_id)
    if _has_column(bind, "digest_log", "org_id"):
        _exec_if_table(bind, "digest_log", "UPDATE digest_log SET org_id = :org_id", org_id=default_org_id)

    if _table_exists(bind, "org_profiles") and _has_column(bind, "org_profiles", "org_id"):
        keep_id = bind.execute(sa.text("SELECT id FROM org_profiles ORDER BY updated_at DESC, id DESC LIMIT 1")).scalar()
        if keep_id:
            op.execute(
                sa.text("DELETE FROM org_profiles WHERE id != :keep_id AND org_id = :org_id").bindparams(
                    keep_id=keep_id,
                    org_id=default_org_id,
                )
            )
            op.execute(sa.text("UPDATE org_profiles SET org_id = :org_id WHERE id = :keep_id").bindparams(org_id=default_org_id, keep_id=keep_id))

    if _table_exists(bind, "organization_memberships"):
        op.execute(
            sa.text(
                """
                INSERT INTO organization_memberships (organization_id, user_id, role, created_at)
                SELECT :org_id, users.id,
                       CASE WHEN users.email = 'admin@example.com' THEN 'admin' ELSE 'member' END,
                       CURRENT_TIMESTAMP
                FROM users
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM organization_memberships m
                    WHERE m.organization_id = :org_id
                      AND m.user_id = users.id
                )
                """
            ).bindparams(org_id=default_org_id)
        )


def downgrade() -> None:
    pass
