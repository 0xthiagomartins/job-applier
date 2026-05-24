"""Add resume mode and target metadata to application submissions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260522_000003"
down_revision = "20260328_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_submissions",
        sa.Column(
            "resume_mode",
            sa.String(length=32),
            nullable=False,
            server_default="static",
        ),
    )
    op.add_column(
        "application_submissions",
        sa.Column("matched_role_target", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "application_submissions",
        sa.Column(
            "matched_specializations",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.create_index(
        op.f("ix_application_submissions_resume_mode"),
        "application_submissions",
        ["resume_mode"],
        unique=False,
    )
    op.create_index(
        op.f("ix_application_submissions_matched_role_target"),
        "application_submissions",
        ["matched_role_target"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_application_submissions_matched_role_target"),
        table_name="application_submissions",
    )
    op.drop_index(
        op.f("ix_application_submissions_resume_mode"),
        table_name="application_submissions",
    )
    op.drop_column("application_submissions", "matched_specializations")
    op.drop_column("application_submissions", "matched_role_target")
    op.drop_column("application_submissions", "resume_mode")
