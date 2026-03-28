"""Initial SQLite schema for persistence repositories."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260328_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_postings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("external_job_id", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("workplace_type", sa.String(length=32), nullable=True),
        sa.Column("seniority", sa.String(length=32), nullable=True),
        sa.Column("description_raw", sa.Text(), nullable=False),
        sa.Column("description_hash", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_postings")),
    )
    op.create_index(
        op.f("ix_job_postings_captured_at"), "job_postings", ["captured_at"], unique=False
    )
    op.create_index(
        op.f("ix_job_postings_company_name"),
        "job_postings",
        ["company_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_postings_external_job_id"),
        "job_postings",
        ["external_job_id"],
        unique=False,
    )
    op.create_index(op.f("ix_job_postings_title"), "job_postings", ["title"], unique=False)

    op.create_table(
        "profile_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_snapshots")),
    )
    op.create_index(
        op.f("ix_profile_snapshots_created_at"),
        "profile_snapshots",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "application_submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_posting_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cv_version", sa.String(length=255), nullable=True),
        sa.Column("cover_letter_version", sa.String(length=255), nullable=True),
        sa.Column("profile_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("ruleset_version", sa.String(length=255), nullable=True),
        sa.Column("ai_model_used", sa.String(length=255), nullable=True),
        sa.Column("execution_origin", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
            name=op.f("fk_application_submissions_job_posting_id_job_postings"),
        ),
        sa.ForeignKeyConstraint(
            ["profile_snapshot_id"],
            ["profile_snapshots.id"],
            name=op.f("fk_application_submissions_profile_snapshot_id_profile_snapshots"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_submissions")),
    )
    op.create_index(
        op.f("ix_application_submissions_execution_origin"),
        "application_submissions",
        ["execution_origin"],
        unique=False,
    )
    op.create_index(
        op.f("ix_application_submissions_job_posting_id"),
        "application_submissions",
        ["job_posting_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_application_submissions_profile_snapshot_id"),
        "application_submissions",
        ["profile_snapshot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_application_submissions_status"),
        "application_submissions",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_application_submissions_submitted_at"),
        "application_submissions",
        ["submitted_at"],
        unique=False,
    )

    op.create_table(
        "application_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("question_raw", sa.Text(), nullable=False),
        sa.Column("question_type", sa.String(length=64), nullable=False),
        sa.Column("normalized_key", sa.String(length=255), nullable=False),
        sa.Column("answer_raw", sa.Text(), nullable=False),
        sa.Column("answer_source", sa.String(length=64), nullable=False),
        sa.Column("fill_strategy", sa.String(length=64), nullable=False),
        sa.Column("ambiguity_flag", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["application_submissions.id"],
            name=op.f("fk_application_answers_submission_id_application_submissions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_answers")),
    )
    op.create_index(
        op.f("ix_application_answers_normalized_key"),
        "application_answers",
        ["normalized_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_application_answers_submission_id"),
        "application_answers",
        ["submission_id"],
        unique=False,
    )

    op.create_table(
        "recruiter_interactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("recruiter_name", sa.String(length=255), nullable=False),
        sa.Column("recruiter_profile_url", sa.Text(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message_sent", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["application_submissions.id"],
            name=op.f("fk_recruiter_interactions_submission_id_application_submissions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recruiter_interactions")),
    )
    op.create_index(
        op.f("ix_recruiter_interactions_status"),
        "recruiter_interactions",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_recruiter_interactions_submission_id"),
        "recruiter_interactions",
        ["submission_id"],
        unique=False,
    )

    op.create_table(
        "execution_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["application_submissions.id"],
            name=op.f("fk_execution_events_submission_id_application_submissions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_events")),
    )
    op.create_index(
        op.f("ix_execution_events_event_type"),
        "execution_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_execution_events_execution_id"),
        "execution_events",
        ["execution_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_execution_events_submission_id"),
        "execution_events",
        ["submission_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_execution_events_timestamp"),
        "execution_events",
        ["timestamp"],
        unique=False,
    )

    op.create_table(
        "artifact_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["application_submissions.id"],
            name=op.f("fk_artifact_snapshots_submission_id_application_submissions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact_snapshots")),
    )
    op.create_index(
        op.f("ix_artifact_snapshots_artifact_type"),
        "artifact_snapshots",
        ["artifact_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_artifact_snapshots_created_at"),
        "artifact_snapshots",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_artifact_snapshots_submission_id"),
        "artifact_snapshots",
        ["submission_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_artifact_snapshots_submission_id"), table_name="artifact_snapshots")
    op.drop_index(op.f("ix_artifact_snapshots_created_at"), table_name="artifact_snapshots")
    op.drop_index(op.f("ix_artifact_snapshots_artifact_type"), table_name="artifact_snapshots")
    op.drop_table("artifact_snapshots")

    op.drop_index(op.f("ix_execution_events_timestamp"), table_name="execution_events")
    op.drop_index(op.f("ix_execution_events_submission_id"), table_name="execution_events")
    op.drop_index(op.f("ix_execution_events_execution_id"), table_name="execution_events")
    op.drop_index(op.f("ix_execution_events_event_type"), table_name="execution_events")
    op.drop_table("execution_events")

    op.drop_index(
        op.f("ix_recruiter_interactions_submission_id"), table_name="recruiter_interactions"
    )
    op.drop_index(op.f("ix_recruiter_interactions_status"), table_name="recruiter_interactions")
    op.drop_table("recruiter_interactions")

    op.drop_index(op.f("ix_application_answers_submission_id"), table_name="application_answers")
    op.drop_index(op.f("ix_application_answers_normalized_key"), table_name="application_answers")
    op.drop_table("application_answers")

    op.drop_index(
        op.f("ix_application_submissions_submitted_at"),
        table_name="application_submissions",
    )
    op.drop_index(
        op.f("ix_application_submissions_status"),
        table_name="application_submissions",
    )
    op.drop_index(
        op.f("ix_application_submissions_profile_snapshot_id"),
        table_name="application_submissions",
    )
    op.drop_index(
        op.f("ix_application_submissions_job_posting_id"),
        table_name="application_submissions",
    )
    op.drop_index(
        op.f("ix_application_submissions_execution_origin"),
        table_name="application_submissions",
    )
    op.drop_table("application_submissions")

    op.drop_index(op.f("ix_profile_snapshots_created_at"), table_name="profile_snapshots")
    op.drop_table("profile_snapshots")

    op.drop_index(op.f("ix_job_postings_title"), table_name="job_postings")
    op.drop_index(op.f("ix_job_postings_external_job_id"), table_name="job_postings")
    op.drop_index(op.f("ix_job_postings_company_name"), table_name="job_postings")
    op.drop_index(op.f("ix_job_postings_captured_at"), table_name="job_postings")
    op.drop_table("job_postings")
