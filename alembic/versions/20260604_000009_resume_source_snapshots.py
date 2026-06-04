"""Add persisted canonical resume source snapshots."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260604_000009"
down_revision = "20260603_000008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resume_source_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_key", sa.String(length=255), nullable=False),
        sa.Column("cv_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_cv_filename", sa.String(length=255), nullable=True),
        sa.Column("source_cv_path", sa.Text(), nullable=True),
        sa.Column("source_resume_text", sa.Text(), nullable=True),
        sa.Column("source_resume_language", sa.String(length=8), nullable=False),
        sa.Column("snapshot_schema_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_origin", sa.String(length=64), nullable=False),
        sa.Column("user_edited", sa.Boolean(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_resume_source_snapshots_owner_key",
        "resume_source_snapshots",
        ["owner_key"],
        unique=False,
    )
    op.create_index(
        "ix_resume_source_snapshots_cv_sha256",
        "resume_source_snapshots",
        ["cv_sha256"],
        unique=False,
    )
    op.create_index(
        "ix_resume_source_snapshots_updated_at",
        "resume_source_snapshots",
        ["updated_at"],
        unique=False,
    )
    op.create_index(
        "ux_resume_source_snapshots_owner_key_cv_sha256",
        "resume_source_snapshots",
        ["owner_key", "cv_sha256"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ux_resume_source_snapshots_owner_key_cv_sha256",
        table_name="resume_source_snapshots",
    )
    op.drop_index("ix_resume_source_snapshots_updated_at", table_name="resume_source_snapshots")
    op.drop_index("ix_resume_source_snapshots_cv_sha256", table_name="resume_source_snapshots")
    op.drop_index("ix_resume_source_snapshots_owner_key", table_name="resume_source_snapshots")
    op.drop_table("resume_source_snapshots")
