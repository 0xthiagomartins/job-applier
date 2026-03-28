"""SQLAlchemy models for the MVP persistence layer."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from job_applier.infrastructure.sqlite.database import Base


class JobPostingModel(Base):
    """Persisted job posting."""

    __tablename__ = "job_postings"
    __table_args__ = (
        Index("ix_job_postings_company_name", "company_name"),
        Index("ix_job_postings_title", "title"),
        Index("ix_job_postings_external_job_id", "external_job_id"),
        Index("ix_job_postings_captured_at", "captured_at"),
        Index(
            "ux_job_postings_platform_external_job_id",
            "platform",
            "external_job_id",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    external_job_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str | None] = mapped_column(String(255))
    workplace_type: Mapped[str | None] = mapped_column(String(32))
    seniority: Mapped[str | None] = mapped_column(String(32))
    easy_apply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description_raw: Mapped[str] = mapped_column(Text, nullable=False)
    description_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    submissions: Mapped[list[ApplicationSubmissionModel]] = relationship(
        back_populates="job_posting",
    )


class ProfileSnapshotModel(Base):
    """Persisted immutable snapshot."""

    __tablename__ = "profile_snapshots"
    __table_args__ = (Index("ix_profile_snapshots_created_at", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    submissions: Mapped[list[ApplicationSubmissionModel]] = relationship(
        back_populates="profile_snapshot",
    )


class ApplicationSubmissionModel(Base):
    """Persisted submission attempt."""

    __tablename__ = "application_submissions"
    __table_args__ = (
        Index("ix_application_submissions_job_posting_id", "job_posting_id"),
        Index("ix_application_submissions_profile_snapshot_id", "profile_snapshot_id"),
        Index("ix_application_submissions_status", "status"),
        Index("ix_application_submissions_submitted_at", "submitted_at"),
        Index("ix_application_submissions_execution_origin", "execution_origin"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    job_posting_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("job_postings.id"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cv_version: Mapped[str | None] = mapped_column(String(255))
    cover_letter_version: Mapped[str | None] = mapped_column(String(255))
    profile_snapshot_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("profile_snapshots.id"),
    )
    ruleset_version: Mapped[str | None] = mapped_column(String(255))
    ai_model_used: Mapped[str | None] = mapped_column(String(255))
    execution_origin: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    job_posting: Mapped[JobPostingModel] = relationship(back_populates="submissions")
    profile_snapshot: Mapped[ProfileSnapshotModel | None] = relationship(
        back_populates="submissions",
    )
    answers: Mapped[list[ApplicationAnswerModel]] = relationship(back_populates="submission")
    recruiter_interactions: Mapped[list[RecruiterInteractionModel]] = relationship(
        back_populates="submission",
    )
    execution_events: Mapped[list[ExecutionEventModel]] = relationship(back_populates="submission")
    artifact_snapshots: Mapped[list[ArtifactSnapshotModel]] = relationship(
        back_populates="submission",
    )


class ApplicationAnswerModel(Base):
    """Persisted answer sent during an application."""

    __tablename__ = "application_answers"
    __table_args__ = (
        Index("ix_application_answers_submission_id", "submission_id"),
        Index("ix_application_answers_normalized_key", "normalized_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("application_submissions.id"),
        nullable=False,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    question_raw: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_key: Mapped[str] = mapped_column(String(255), nullable=False)
    answer_raw: Mapped[str] = mapped_column(Text, nullable=False)
    answer_source: Mapped[str] = mapped_column(String(64), nullable=False)
    fill_strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    ambiguity_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    submission: Mapped[ApplicationSubmissionModel] = relationship(back_populates="answers")


class RecruiterInteractionModel(Base):
    """Persisted recruiter interaction."""

    __tablename__ = "recruiter_interactions"
    __table_args__ = (
        Index("ix_recruiter_interactions_submission_id", "submission_id"),
        Index("ix_recruiter_interactions_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("application_submissions.id"),
        nullable=False,
    )
    recruiter_name: Mapped[str] = mapped_column(String(255), nullable=False)
    recruiter_profile_url: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message_sent: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    submission: Mapped[ApplicationSubmissionModel] = relationship(
        back_populates="recruiter_interactions",
    )


class ExecutionEventModel(Base):
    """Persisted execution event."""

    __tablename__ = "execution_events"
    __table_args__ = (
        Index("ix_execution_events_execution_id", "execution_id"),
        Index("ix_execution_events_submission_id", "submission_id"),
        Index("ix_execution_events_timestamp", "timestamp"),
        Index("ix_execution_events_event_type", "event_type"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    execution_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    submission_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("application_submissions.id"),
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    submission: Mapped[ApplicationSubmissionModel | None] = relationship(
        back_populates="execution_events",
    )


class ArtifactSnapshotModel(Base):
    """Persisted artifact created during an execution."""

    __tablename__ = "artifact_snapshots"
    __table_args__ = (
        Index("ix_artifact_snapshots_submission_id", "submission_id"),
        Index("ix_artifact_snapshots_artifact_type", "artifact_type"),
        Index("ix_artifact_snapshots_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("application_submissions.id"),
        nullable=False,
    )
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    submission: Mapped[ApplicationSubmissionModel] = relationship(
        back_populates="artifact_snapshots",
    )
