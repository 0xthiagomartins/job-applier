"""Add description-quality score to job postings."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260527_000006"
down_revision = "20260527_000005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_postings",
        sa.Column("detail_description_score", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("job_postings", "detail_description_score")
