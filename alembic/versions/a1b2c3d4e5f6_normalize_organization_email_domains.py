"""normalize organization email domains

Revision ID: a1b2c3d4e5f6
Revises: f4a5b6c7d8e9
Create Date: 2026-06-10 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


PUBLIC_EMAIL_DOMAINS = (
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "icloud.com",
)


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    if table_name not in sa.inspect(bind).get_table_names():
        return False
    return column_name in {col["name"] for col in sa.inspect(bind).get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("organizations", "email_domain"):
        return

    op.execute(sa.text("UPDATE organizations SET email_domain = lower(trim(email_domain)) WHERE email_domain IS NOT NULL"))
    op.execute(
        sa.text(
            """
            UPDATE organizations
            SET email_domain = NULL
            WHERE email_domain = ''
               OR email_domain IN :public_domains
            """
        ).bindparams(sa.bindparam("public_domains", expanding=True, value=PUBLIC_EMAIL_DOMAINS))
    )


def downgrade() -> None:
    pass
