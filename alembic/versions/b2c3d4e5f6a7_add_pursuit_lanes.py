"""add pursuit lanes

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _index_exists(inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "pursuit_lanes"):
        op.create_table(
            "pursuit_lanes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("agencies", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("naics", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("keywords", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("set_asides", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_pursuit_lanes_id"), "pursuit_lanes", ["id"], unique=False)
        op.create_index(op.f("ix_pursuit_lanes_organization_id"), "pursuit_lanes", ["organization_id"], unique=False)

    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "pursuit_lane_assignments"):
        op.create_table(
            "pursuit_lane_assignments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("pursuit_lane_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.ForeignKeyConstraint(["pursuit_lane_id"], ["pursuit_lanes.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id", "pursuit_lane_id", "user_id", name="uq_lane_assignment"),
        )
        op.create_index(op.f("ix_pursuit_lane_assignments_id"), "pursuit_lane_assignments", ["id"], unique=False)
        op.create_index(op.f("ix_pursuit_lane_assignments_organization_id"), "pursuit_lane_assignments", ["organization_id"], unique=False)
        op.create_index(op.f("ix_pursuit_lane_assignments_pursuit_lane_id"), "pursuit_lane_assignments", ["pursuit_lane_id"], unique=False)
        op.create_index(op.f("ix_pursuit_lane_assignments_user_id"), "pursuit_lane_assignments", ["user_id"], unique=False)

    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "opportunity_pursuit_lane_matches"):
        op.create_table(
            "opportunity_pursuit_lane_matches",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("opportunity_id", sa.Integer(), nullable=False),
            sa.Column("pursuit_lane_id", sa.Integer(), nullable=False),
            sa.Column("matched_reasons", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
            sa.ForeignKeyConstraint(["pursuit_lane_id"], ["pursuit_lanes.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id", "opportunity_id", "pursuit_lane_id", name="uq_opp_lane_match"),
        )
        op.create_index(op.f("ix_opportunity_pursuit_lane_matches_id"), "opportunity_pursuit_lane_matches", ["id"], unique=False)
        op.create_index(op.f("ix_opportunity_pursuit_lane_matches_organization_id"), "opportunity_pursuit_lane_matches", ["organization_id"], unique=False)
        op.create_index(op.f("ix_opportunity_pursuit_lane_matches_opportunity_id"), "opportunity_pursuit_lane_matches", ["opportunity_id"], unique=False)
        op.create_index(op.f("ix_opportunity_pursuit_lane_matches_pursuit_lane_id"), "opportunity_pursuit_lane_matches", ["pursuit_lane_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _table_exists(inspector, "opportunity_pursuit_lane_matches"):
        op.drop_table("opportunity_pursuit_lane_matches")
    inspector = sa.inspect(bind)
    if _table_exists(inspector, "pursuit_lane_assignments"):
        op.drop_table("pursuit_lane_assignments")
    inspector = sa.inspect(bind)
    if _table_exists(inspector, "pursuit_lanes"):
        op.drop_table("pursuit_lanes")
