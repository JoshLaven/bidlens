"""add company profiles

Revision ID: a7c9d2f4b6e1
Revises: f2b3641e4d7a
Create Date: 2026-06-05 03:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "a7c9d2f4b6e1"
down_revision = "f2b3641e4d7a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=True),
        sa.Column("website_url", sa.String(), nullable=True),
        sa.Column("cage_code", sa.String(), nullable=True),
        sa.Column("duns", sa.String(), nullable=True),
        sa.Column("uei", sa.String(), nullable=True),
        sa.Column("profile_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_company_profiles_id"), "company_profiles", ["id"], unique=False)
    op.create_index(op.f("ix_company_profiles_org_id"), "company_profiles", ["org_id"], unique=False)
    op.create_index(op.f("ix_company_profiles_company_name"), "company_profiles", ["company_name"], unique=False)
    op.create_index(op.f("ix_company_profiles_cage_code"), "company_profiles", ["cage_code"], unique=False)
    op.create_index(op.f("ix_company_profiles_duns"), "company_profiles", ["duns"], unique=False)
    op.create_index(op.f("ix_company_profiles_uei"), "company_profiles", ["uei"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_company_profiles_uei"), table_name="company_profiles")
    op.drop_index(op.f("ix_company_profiles_duns"), table_name="company_profiles")
    op.drop_index(op.f("ix_company_profiles_cage_code"), table_name="company_profiles")
    op.drop_index(op.f("ix_company_profiles_company_name"), table_name="company_profiles")
    op.drop_index(op.f("ix_company_profiles_org_id"), table_name="company_profiles")
    op.drop_index(op.f("ix_company_profiles_id"), table_name="company_profiles")
    op.drop_table("company_profiles")
