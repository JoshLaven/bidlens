"""add named SAM.gov saved searches

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
"""

from alembic import op
import sqlalchemy as sa


revision = "b1c2d3e4f5a6"
down_revision = "a0b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sam_source_configs", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column(
                "name",
                sa.String(),
                nullable=False,
                server_default="Default SAM.gov Search",
            )
        )
        batch_op.drop_constraint("uq_sam_source_config_org", type_="unique")
        batch_op.create_unique_constraint(
            "uq_sam_source_config_org_name",
            ["organization_id", "name"],
        )


def downgrade():
    # Keep the oldest saved search for each workspace before restoring the
    # original one-search-per-workspace constraint.
    connection = op.get_bind()
    duplicate_ids = connection.execute(
        sa.text(
            """
            SELECT newer.id
            FROM sam_source_configs AS newer
            JOIN sam_source_configs AS older
              ON older.organization_id = newer.organization_id
             AND older.id < newer.id
            """
        )
    ).scalars().all()
    if duplicate_ids:
        connection.execute(
            sa.text("DELETE FROM sam_source_configs WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"ids": duplicate_ids},
        )

    with op.batch_alter_table("sam_source_configs", recreate="always") as batch_op:
        batch_op.drop_constraint("uq_sam_source_config_org_name", type_="unique")
        batch_op.drop_column("name")
        batch_op.create_unique_constraint(
            "uq_sam_source_config_org",
            ["organization_id"],
        )
