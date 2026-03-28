"""Core domain entities for Job Applier."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from job_applier.domain.enums import (
    AnswerSource,
    ArtifactType,
    ExecutionEventType,
    ExecutionOrigin,
    FillStrategy,
    Platform,
    QuestionType,
    RecruiterAction,
    RecruiterInteractionStatus,
    SeniorityLevel,
    SubmissionStatus,
    WorkplaceType,
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def ensure_utc(value: datetime, field_name: str) -> datetime:
    """Normalize and validate timestamps in UTC."""

    if value.tzinfo is None:
        msg = f"{field_name} must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


def ensure_non_empty(value: str, field_name: str) -> str:
    """Validate string fields that cannot be blank."""

    stripped = value.strip()
    if not stripped:
        msg = f"{field_name} cannot be blank"
        raise ValueError(msg)
    return stripped


@dataclass(frozen=True, slots=True, kw_only=True)
class JobPosting:
    """Represents a job posting captured from a platform."""

    platform: Platform
    url: str
    title: str
    company_name: str
    description_raw: str
    id: UUID = field(default_factory=uuid4)
    external_job_id: str | None = None
    location: str | None = None
    workplace_type: WorkplaceType | None = None
    seniority: SeniorityLevel | None = None
    description_hash: str = ""
    captured_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", ensure_non_empty(self.url, "url"))
        object.__setattr__(self, "title", ensure_non_empty(self.title, "title"))
        object.__setattr__(
            self,
            "company_name",
            ensure_non_empty(self.company_name, "company_name"),
        )
        object.__setattr__(
            self,
            "description_raw",
            ensure_non_empty(self.description_raw, "description_raw"),
        )
        object.__setattr__(self, "captured_at", ensure_utc(self.captured_at, "captured_at"))
        if self.description_hash:
            object.__setattr__(
                self,
                "description_hash",
                ensure_non_empty(self.description_hash, "description_hash"),
            )
        else:
            digest = sha256(self.description_raw.encode("utf-8")).hexdigest()
            object.__setattr__(self, "description_hash", digest)


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationSubmission:
    """Represents a job application submission."""

    job_posting_id: UUID
    id: UUID = field(default_factory=uuid4)
    status: SubmissionStatus = SubmissionStatus.PENDING
    started_at: datetime = field(default_factory=utc_now)
    submitted_at: datetime | None = None
    cv_version: str | None = None
    cover_letter_version: str | None = None
    profile_snapshot_id: UUID | None = None
    ruleset_version: str | None = None
    ai_model_used: str | None = None
    execution_origin: ExecutionOrigin = ExecutionOrigin.SCHEDULED
    notes: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", ensure_utc(self.started_at, "started_at"))
        if self.submitted_at is not None:
            object.__setattr__(
                self,
                "submitted_at",
                ensure_utc(self.submitted_at, "submitted_at"),
            )

        if self.status is SubmissionStatus.SUBMITTED:
            missing_fields = [
                field_name
                for field_name, field_value in (
                    ("submitted_at", self.submitted_at),
                    ("profile_snapshot_id", self.profile_snapshot_id),
                    ("ruleset_version", self.ruleset_version),
                )
                if field_value is None
            ]
            if missing_fields:
                joined = ", ".join(missing_fields)
                msg = f"submitted submissions require: {joined}"
                raise ValueError(msg)
        elif self.submitted_at is not None:
            msg = "submitted_at can only be set when status is submitted"
            raise ValueError(msg)

        if self.ruleset_version is not None:
            object.__setattr__(
                self,
                "ruleset_version",
                ensure_non_empty(self.ruleset_version, "ruleset_version"),
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationAnswer:
    """Represents a normalized question/answer pair sent during submission."""

    submission_id: UUID
    step_index: int
    question_raw: str
    question_type: QuestionType
    normalized_key: str
    answer_raw: str
    answer_source: AnswerSource
    fill_strategy: FillStrategy
    id: UUID = field(default_factory=uuid4)
    ambiguity_flag: bool = False

    def __post_init__(self) -> None:
        if self.step_index < 0:
            msg = "step_index must be zero or greater"
            raise ValueError(msg)
        object.__setattr__(
            self,
            "question_raw",
            ensure_non_empty(self.question_raw, "question_raw"),
        )
        object.__setattr__(
            self,
            "normalized_key",
            ensure_non_empty(self.normalized_key, "normalized_key"),
        )
        object.__setattr__(self, "answer_raw", ensure_non_empty(self.answer_raw, "answer_raw"))
        if self.fill_strategy is FillStrategy.BEST_EFFORT and not self.ambiguity_flag:
            msg = "best-effort answers must be marked as ambiguous"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileSnapshot:
    """Represents an immutable snapshot of the configuration used in a submission."""

    data_json: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, "created_at"))
        object.__setattr__(self, "data_json", ensure_non_empty(self.data_json, "data_json"))
        json.loads(self.data_json)


@dataclass(frozen=True, slots=True, kw_only=True)
class RecruiterInteraction:
    """Represents an attempted recruiter interaction for a submission."""

    submission_id: UUID
    recruiter_name: str
    action: RecruiterAction
    status: RecruiterInteractionStatus
    id: UUID = field(default_factory=uuid4)
    recruiter_profile_url: str | None = None
    message_sent: str | None = None
    sent_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "recruiter_name",
            ensure_non_empty(self.recruiter_name, "recruiter_name"),
        )
        if self.sent_at is not None:
            object.__setattr__(self, "sent_at", ensure_utc(self.sent_at, "sent_at"))
        if self.status is RecruiterInteractionStatus.SENT and self.sent_at is None:
            msg = "sent recruiter interactions require sent_at"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionEvent:
    """Represents an execution event emitted during automation."""

    submission_id: UUID
    event_type: ExecutionEventType
    payload_json: str
    id: UUID = field(default_factory=uuid4)
    timestamp: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", ensure_utc(self.timestamp, "timestamp"))
        object.__setattr__(
            self,
            "payload_json",
            ensure_non_empty(self.payload_json, "payload_json"),
        )
        json.loads(self.payload_json)


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactSnapshot:
    """Represents an execution artifact linked to a submission."""

    submission_id: UUID
    artifact_type: ArtifactType
    path: str
    sha256: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", ensure_non_empty(self.path, "path"))
        object.__setattr__(self, "sha256", ensure_non_empty(self.sha256, "sha256"))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, "created_at"))
