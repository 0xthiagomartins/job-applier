"""Add job posting search fields used by the LinkedIn fetch flow."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260328_000002"
down_revision = "20260328_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_postings",
        sa.Column("easy_apply", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index(
        "ux_job_postings_platform_external_job_id",
        "job_postings",
        ["platform", "external_job_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_job_postings_platform_external_job_id", table_name="job_postings")
    op.drop_column("job_postings", "easy_apply")
