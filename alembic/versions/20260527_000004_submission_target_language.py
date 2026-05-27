"""Add target language metadata to application submissions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260527_000004"
down_revision = "20260522_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_submissions",
        sa.Column(
            "target_language",
            sa.String(length=8),
            nullable=False,
            server_default="en",
        ),
    )
    op.create_index(
        op.f("ix_application_submissions_target_language"),
        "application_submissions",
        ["target_language"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_application_submissions_target_language"),
        table_name="application_submissions",
    )
    op.drop_column("application_submissions", "target_language")
