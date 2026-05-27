"""Add detail quality metadata to job postings."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260527_000005"
down_revision = "20260527_000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_postings",
        sa.Column("detail_quality_score", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "job_postings",
        sa.Column(
            "detail_quality_source",
            sa.String(length=64),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "job_postings",
        sa.Column(
            "detail_quality_signals",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("job_postings", "detail_quality_signals")
    op.drop_column("job_postings", "detail_quality_source")
    op.drop_column("job_postings", "detail_quality_score")
