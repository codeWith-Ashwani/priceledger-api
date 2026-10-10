"""add sourced retailer ratings to offers"""

import sqlalchemy as sa
from alembic import op

revision = "4c1ecca0d446"
down_revision = "f51bf5cc179e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("offers", sa.Column("rating", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("offers", "rating")
