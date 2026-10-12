"""add offer reports and private review audit"""

import sqlalchemy as sa
from alembic import op

revision = "c627df3aa610"
down_revision = "79f240a1ce38"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "offer_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), unique=True, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("offer_id", sa.String(120), sa.ForeignKey("offers.id"), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("details", sa.String(1000), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'in_review', 'resolved', 'dismissed')", name="report_status"
        ),
        sa.CheckConstraint("revision >= 1", name="report_revision"),
    )
    op.create_index(
        "ix_offer_reports_status_created", "offer_reports", ["status", "created_at", "id"]
    )
    op.create_index("ix_offer_reports_offer_created", "offer_reports", ["offer_id", "created_at"])
    op.create_table(
        "report_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_id", sa.String(36), sa.ForeignKey("offer_reports.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(20), nullable=True),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("note", sa.String(1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("report_id", "revision"),
    )


def downgrade():
    op.drop_table("report_events")
    op.drop_index("ix_offer_reports_offer_created", table_name="offer_reports")
    op.drop_index("ix_offer_reports_status_created", table_name="offer_reports")
    op.drop_table("offer_reports")
