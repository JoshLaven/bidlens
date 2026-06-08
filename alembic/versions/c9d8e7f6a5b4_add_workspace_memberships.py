"""add workspace memberships

Revision ID: c9d8e7f6a5b4
Revises: a7c9d2f4b6e1
Create Date: 2026-06-07 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c9d8e7f6a5b4"
down_revision = "a7c9d2f4b6e1"
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _ensure_column(inspector, table_name: str, column: sa.Column) -> None:
    if _table_exists(inspector, table_name) and not _has_column(inspector, table_name, column.name):
        op.add_column(table_name, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "organizations"):
        op.create_table(
            "organizations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("slug", sa.String(), nullable=False),
            sa.Column("email_domain", sa.String(), nullable=True),
            sa.Column("plan", sa.String(), nullable=False, server_default="free"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_organizations_id"), "organizations", ["id"], unique=False)
    else:
        _ensure_column(inspector, "organizations", sa.Column("slug", sa.String(), nullable=True))
        _ensure_column(inspector, "organizations", sa.Column("email_domain", sa.String(), nullable=True))

    inspector = sa.inspect(bind)
    if _table_exists(inspector, "organizations"):
        if not any(idx["name"] == "ix_organizations_slug" for idx in inspector.get_indexes("organizations")):
            op.create_index(op.f("ix_organizations_slug"), "organizations", ["slug"], unique=True)
        if not any(idx["name"] == "ix_organizations_email_domain" for idx in inspector.get_indexes("organizations")):
            op.create_index(op.f("ix_organizations_email_domain"), "organizations", ["email_domain"], unique=False)

    if not _table_exists(inspector, "users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("email", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=True),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_users_id"), "users", ["id"], unique=False)
        op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    else:
        _ensure_column(inspector, "users", sa.Column("name", sa.String(), nullable=True))

    if not _table_exists(inspector, "organization_memberships"):
        op.create_table(
            "organization_memberships",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(), nullable=False, server_default="member"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id", "user_id", name="uq_org_membership"),
        )
        op.create_index(op.f("ix_organization_memberships_id"), "organization_memberships", ["id"], unique=False)
        op.create_index(op.f("ix_organization_memberships_organization_id"), "organization_memberships", ["organization_id"], unique=False)
        op.create_index(op.f("ix_organization_memberships_user_id"), "organization_memberships", ["user_id"], unique=False)

    inspector = sa.inspect(bind)
    _ensure_column(inspector, "opportunities", sa.Column("organization_id", sa.Integer(), nullable=True))
    _ensure_column(inspector, "opportunity_briefs", sa.Column("organization_id", sa.Integer(), nullable=True))
    _ensure_column(inspector, "user_opportunities", sa.Column("organization_id", sa.Integer(), nullable=True))

    op.execute(
        sa.text(
            """
            INSERT INTO organizations (name, slug, email_domain, plan, is_active, created_at)
            SELECT 'Default Workspace', 'default-workspace', NULL, 'free', 1, CURRENT_TIMESTAMP
            WHERE NOT EXISTS (SELECT 1 FROM organizations WHERE slug = 'default-workspace')
            """
        )
    )
    default_org_id = bind.execute(
        sa.text("SELECT id FROM organizations WHERE slug = 'default-workspace' ORDER BY id ASC LIMIT 1")
    ).scalar()

    op.execute(
        sa.text(
            """
            UPDATE organizations
            SET slug = 'workspace-' || id
            WHERE slug IS NULL OR slug = ''
            """
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO users (email, name, organization_id, created_at)
            SELECT 'admin@example.com', NULL, :org_id, CURRENT_TIMESTAMP
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = 'admin@example.com')
            """
        ).bindparams(org_id=default_org_id)
    )

    admin_user_id = bind.execute(
        sa.text("SELECT id FROM users WHERE email = 'admin@example.com' ORDER BY id ASC LIMIT 1")
    ).scalar()

    if _has_column(sa.inspect(bind), "users", "organization_id"):
        op.execute(
            sa.text("UPDATE users SET organization_id = :org_id WHERE organization_id IS NULL").bindparams(org_id=default_org_id)
        )

    op.execute(
        sa.text(
            """
            INSERT INTO organization_memberships (organization_id, user_id, role, created_at)
            SELECT users.organization_id, users.id,
                   CASE WHEN users.id = :admin_user_id THEN 'admin' ELSE 'member' END,
                   CURRENT_TIMESTAMP
            FROM users
            WHERE users.organization_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM organization_memberships m
                  WHERE m.organization_id = users.organization_id
                    AND m.user_id = users.id
              )
            """
        ).bindparams(admin_user_id=admin_user_id)
    )

    op.execute(
        sa.text(
            """
            INSERT INTO organization_memberships (organization_id, user_id, role, created_at)
            SELECT :org_id, :user_id, 'admin', CURRENT_TIMESTAMP
            WHERE NOT EXISTS (
                SELECT 1 FROM organization_memberships
                WHERE organization_id = :org_id AND user_id = :user_id
            )
            """
        ).bindparams(org_id=default_org_id, user_id=admin_user_id)
    )

    inspector = sa.inspect(bind)
    if _has_column(inspector, "opportunities", "organization_id"):
        op.execute(sa.text("UPDATE opportunities SET organization_id = :org_id WHERE organization_id IS NULL").bindparams(org_id=default_org_id))
        op.create_index(op.f("ix_opportunities_organization_id"), "opportunities", ["organization_id"], unique=False)

    if _has_column(inspector, "company_profiles", "org_id"):
        op.execute(sa.text("UPDATE company_profiles SET org_id = :org_id WHERE org_id IS NULL").bindparams(org_id=default_org_id))

    if _has_column(inspector, "opportunity_notes", "org_id"):
        op.execute(sa.text("UPDATE opportunity_notes SET org_id = :org_id WHERE org_id IS NULL").bindparams(org_id=default_org_id))

    if _has_column(inspector, "opportunity_briefs", "organization_id"):
        op.execute(
            sa.text(
                """
                UPDATE opportunity_briefs
                SET organization_id = COALESCE(
                    (SELECT opportunities.organization_id
                     FROM opportunities
                     WHERE opportunities.id = opportunity_briefs.opportunity_id),
                    :org_id
                )
                WHERE organization_id IS NULL
                """
            ).bindparams(org_id=default_org_id)
        )
        op.create_index(op.f("ix_opportunity_briefs_organization_id"), "opportunity_briefs", ["organization_id"], unique=False)

    if _has_column(inspector, "user_opportunities", "organization_id"):
        op.execute(
            sa.text(
                """
                UPDATE user_opportunities
                SET organization_id = COALESCE(
                    (SELECT opportunities.organization_id
                     FROM opportunities
                     WHERE opportunities.id = user_opportunities.opportunity_id),
                    (SELECT users.organization_id
                     FROM users
                     WHERE users.id = user_opportunities.user_id),
                    :org_id
                )
                WHERE organization_id IS NULL
                """
            ).bindparams(org_id=default_org_id)
        )
        op.create_index(op.f("ix_user_opportunities_organization_id"), "user_opportunities", ["organization_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, "user_opportunities", "organization_id"):
        op.drop_index(op.f("ix_user_opportunities_organization_id"), table_name="user_opportunities")
        op.drop_column("user_opportunities", "organization_id")
    if _has_column(inspector, "opportunity_briefs", "organization_id"):
        op.drop_index(op.f("ix_opportunity_briefs_organization_id"), table_name="opportunity_briefs")
        op.drop_column("opportunity_briefs", "organization_id")
    if _has_column(inspector, "opportunities", "organization_id"):
        op.drop_index(op.f("ix_opportunities_organization_id"), table_name="opportunities")
        op.drop_column("opportunities", "organization_id")
    if _table_exists(inspector, "organization_memberships"):
        op.drop_table("organization_memberships")
    if _has_column(inspector, "users", "name"):
        op.drop_column("users", "name")
    if _has_column(inspector, "organizations", "email_domain"):
        op.drop_index(op.f("ix_organizations_email_domain"), table_name="organizations")
        op.drop_column("organizations", "email_domain")
    if _has_column(inspector, "organizations", "slug"):
        op.drop_index(op.f("ix_organizations_slug"), table_name="organizations")
        op.drop_column("organizations", "slug")
