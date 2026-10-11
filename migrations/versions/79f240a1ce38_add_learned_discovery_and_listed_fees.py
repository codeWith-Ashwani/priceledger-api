"""add learned discovery stores and sourced listed fees"""

import sqlalchemy as sa
from alembic import op

revision = "79f240a1ce38"
down_revision = "4c1ecca0d446"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("offers", sa.Column("current_listed_fees", sa.JSON(), nullable=True))
    op.add_column("observations", sa.Column("listed_fees", sa.JSON(), nullable=True))
    op.create_table(
        "discovery_stores",
        sa.Column("retailer_id", sa.String(80), primary_key=True),
        sa.Column(
            "source_id", sa.String(80), sa.ForeignKey("sources.id"), unique=True, nullable=False
        ),
        sa.Column("host", sa.String(253), unique=True, nullable=False),
        sa.Column("brands", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("learned_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("discovery_stores")
    op.drop_column("observations", "listed_fees")
    op.drop_column("offers", "current_listed_fees")
