"""Pydantic schemas used for API and internal flow validation."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AnyUrl, BaseModel, ConfigDict, EmailStr, Field, SecretStr

from job_applier.domain.enums import (
    AgentExecutionStatus,
    AnswerSource,
    ArtifactType,
    ExecutionEventType,
    ExecutionOrigin,
    FillStrategy,
    Platform,
    QuestionType,
    RecruiterAction,
    RecruiterInteractionStatus,
    ScheduleFrequency,
    SeniorityLevel,
    SubmissionStatus,
    WorkplaceType,
)


class ReadSchema(BaseModel):
    """Base schema configured to read from dataclasses and domain objects."""

    model_config = ConfigDict(from_attributes=True)


class JobPostingCreate(BaseModel):
    platform: Platform
    url: AnyUrl
    title: str
    company_name: str
    description_raw: str
    external_job_id: str | None = None
    location: str | None = None
    workplace_type: WorkplaceType | None = None
    seniority: SeniorityLevel | None = None
    easy_apply: bool = True
    description_hash: str | None = None
    captured_at: datetime | None = None


class JobPostingRead(ReadSchema):
    id: UUID
    platform: Platform
    external_job_id: str | None
    url: str
    title: str
    company_name: str
    location: str | None
    workplace_type: WorkplaceType | None
    seniority: SeniorityLevel | None
    easy_apply: bool
    description_raw: str
    description_hash: str
    captured_at: datetime


class ApplicationSubmissionCreate(BaseModel):
    job_posting_id: UUID
    status: SubmissionStatus = SubmissionStatus.PENDING
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    cv_version: str | None = None
    cover_letter_version: str | None = None
    profile_snapshot_id: UUID | None = None
    ruleset_version: str | None = None
    ai_model_used: str | None = None
    execution_origin: ExecutionOrigin = ExecutionOrigin.SCHEDULED
    notes: str | None = None


class ApplicationSubmissionRead(ReadSchema):
    id: UUID
    job_posting_id: UUID
    status: SubmissionStatus
    started_at: datetime
    submitted_at: datetime | None
    cv_version: str | None
    cover_letter_version: str | None
    profile_snapshot_id: UUID | None
    ruleset_version: str | None
    ai_model_used: str | None
    execution_origin: ExecutionOrigin
    notes: str | None


class ApplicationAnswerCreate(BaseModel):
    submission_id: UUID
    step_index: int
    question_raw: str
    question_type: QuestionType
    normalized_key: str
    answer_raw: str
    answer_source: AnswerSource
    ambiguity_flag: bool = False
    fill_strategy: FillStrategy


class ApplicationAnswerRead(ReadSchema):
    id: UUID
    submission_id: UUID
    step_index: int
    question_raw: str
    question_type: QuestionType
    normalized_key: str
    answer_raw: str
    answer_source: AnswerSource
    ambiguity_flag: bool
    fill_strategy: FillStrategy


class ProfileSnapshotCreate(BaseModel):
    data_json: str
    created_at: datetime | None = None


class ProfileSnapshotRead(ReadSchema):
    id: UUID
    created_at: datetime
    data_json: str


class RecruiterInteractionCreate(BaseModel):
    submission_id: UUID
    recruiter_name: str
    recruiter_profile_url: AnyUrl | None = None
    action: RecruiterAction
    message_sent: str | None = None
    status: RecruiterInteractionStatus
    sent_at: datetime | None = None


class RecruiterInteractionRead(ReadSchema):
    id: UUID
    submission_id: UUID
    recruiter_name: str
    recruiter_profile_url: str | None
    action: RecruiterAction
    message_sent: str | None
    status: RecruiterInteractionStatus
    sent_at: datetime | None


class ExecutionEventCreate(BaseModel):
    execution_id: UUID
    submission_id: UUID | None = None
    event_type: ExecutionEventType
    payload_json: str
    timestamp: datetime | None = None


class ExecutionEventRead(ReadSchema):
    id: UUID
    execution_id: UUID
    submission_id: UUID | None
    event_type: ExecutionEventType
    timestamp: datetime
    payload_json: str


class ArtifactSnapshotCreate(BaseModel):
    submission_id: UUID
    artifact_type: ArtifactType
    path: str
    sha256: str
    created_at: datetime | None = None


class ArtifactSnapshotRead(ReadSchema):
    id: UUID
    submission_id: UUID
    artifact_type: ArtifactType
    path: str
    sha256: str
    created_at: datetime


class ProfileSnapshotDetailRead(ProfileSnapshotRead):
    data: dict[str, Any]

    @classmethod
    def from_snapshot(cls, snapshot: Any) -> ProfileSnapshotDetailRead:
        """Return the profile snapshot plus parsed JSON payload."""

        return cls.model_validate(
            {
                **ProfileSnapshotRead.model_validate(snapshot).model_dump(mode="json"),
                "data": json.loads(snapshot.data_json),
            },
        )


class ExecutionEventDetailRead(ExecutionEventRead):
    payload: dict[str, Any]

    @classmethod
    def from_event(cls, event: Any) -> ExecutionEventDetailRead:
        """Return the execution event plus parsed JSON payload."""

        return cls.model_validate(
            {
                **ExecutionEventRead.model_validate(event).model_dump(mode="json"),
                "payload": json.loads(event.payload_json),
            },
        )


class ApplicationHistoryListItemRead(BaseModel):
    id: UUID
    submitted_at: datetime
    company_name: str
    job_title: str
    job_url: str
    location: str | None = None
    external_job_id: str | None = None
    cv_version: str | None = None
    execution_origin: ExecutionOrigin
    notes: str | None = None

    @classmethod
    def from_entry(cls, entry: Any) -> ApplicationHistoryListItemRead:
        """Build the compact list item shown by the history screen."""

        return cls(
            id=entry.submission.id,
            submitted_at=entry.submission.submitted_at,
            company_name=entry.job_posting.company_name,
            job_title=entry.job_posting.title,
            job_url=entry.job_posting.url,
            location=entry.job_posting.location,
            external_job_id=entry.job_posting.external_job_id,
            cv_version=entry.submission.cv_version,
            execution_origin=entry.submission.execution_origin,
            notes=entry.submission.notes,
        )


class ApplicationHistoryDetailRead(BaseModel):
    submission: ApplicationSubmissionRead
    job_posting: JobPostingRead
    answers: tuple[ApplicationAnswerRead, ...]
    profile_snapshot: ProfileSnapshotDetailRead | None = None
    recruiter_interactions: tuple[RecruiterInteractionRead, ...]
    execution_events: tuple[ExecutionEventDetailRead, ...]
    artifacts: tuple[ArtifactSnapshotRead, ...]

    @classmethod
    def from_entry(cls, entry: Any) -> ApplicationHistoryDetailRead:
        """Build the full audit response for one successful application."""

        return cls(
            submission=ApplicationSubmissionRead.model_validate(entry.submission),
            job_posting=JobPostingRead.model_validate(entry.job_posting),
            answers=tuple(ApplicationAnswerRead.model_validate(answer) for answer in entry.answers),
            profile_snapshot=(
                ProfileSnapshotDetailRead.from_snapshot(entry.profile_snapshot)
                if entry.profile_snapshot
                else None
            ),
            recruiter_interactions=tuple(
                RecruiterInteractionRead.model_validate(item)
                for item in entry.recruiter_interactions
            ),
            execution_events=tuple(
                ExecutionEventDetailRead.from_event(item) for item in entry.execution_events
            ),
            artifacts=tuple(
                ArtifactSnapshotRead.model_validate(artifact) for artifact in entry.artifacts
            ),
        )


class ApplicationHistoryPageRead(BaseModel):
    items: tuple[ApplicationHistoryListItemRead, ...]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_page(cls, page: Any) -> ApplicationHistoryPageRead:
        """Build the paginated API response for the history list."""

        return cls(
            items=tuple(ApplicationHistoryListItemRead.from_entry(item) for item in page.items),
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )


class ApplicationHistoryDetailEnvelope(BaseModel):
    application: ApplicationHistoryDetailRead


class UserProfileConfigSchema(BaseModel):
    name: str
    email: EmailStr
    phone: str
    city: str
    linkedin_url: AnyUrl | None = None
    github_url: AnyUrl | None = None
    portfolio_url: AnyUrl | None = None
    years_experience_by_stack: dict[str, int] = Field(default_factory=dict)
    work_authorized: bool
    needs_sponsorship: bool = False
    salary_expectation: int | None = None
    availability: str
    default_responses: dict[str, str] = Field(default_factory=dict)
    cv_path: str | None = None
    cv_filename: str | None = None
    positive_filters: tuple[str, ...] = ()
    blacklist: tuple[str, ...] = ()


class SearchConfigSchema(BaseModel):
    keywords: tuple[str, ...]
    location: str
    posted_within_hours: int = 24
    workplace_types: tuple[WorkplaceType, ...] = ()
    seniority: tuple[SeniorityLevel, ...] = ()
    easy_apply_only: bool = True
    minimum_score_threshold: float = Field(default=0.55, ge=0.0, le=1.0)


class ScheduleConfigSchema(BaseModel):
    frequency: ScheduleFrequency = ScheduleFrequency.DAILY
    run_at: str = "23:00"
    timezone: str = "UTC"


class AgentConfigSchema(BaseModel):
    schedule: ScheduleConfigSchema
    auto_connect_with_recruiter: bool = False


class AIConfigWriteSchema(BaseModel):
    model: str
    api_key: SecretStr | None = None


class AIConfigReadSchema(BaseModel):
    model: str


class RulesetConfigSchema(BaseModel):
    version: str = "ruleset-v1"
    allow_best_effort_autofill: bool = True
    auto_connect_with_recruiter: bool = False


class UserAgentConfigWrite(BaseModel):
    config_version: str = "config-v1"
    profile: UserProfileConfigSchema
    search: SearchConfigSchema
    agent: AgentConfigSchema
    ai: AIConfigWriteSchema
    ruleset: RulesetConfigSchema = Field(default_factory=RulesetConfigSchema)


class UserAgentConfigRead(BaseModel):
    config_version: str
    profile: UserProfileConfigSchema
    search: SearchConfigSchema
    agent: AgentConfigSchema
    ai: AIConfigReadSchema
    ruleset: RulesetConfigSchema

    @classmethod
    def from_settings_payload(cls, payload: dict[str, Any]) -> UserAgentConfigRead:
        """Build a read-safe config schema from a settings snapshot payload."""

        return cls.model_validate(payload)


class AgentExecutionSummaryRead(BaseModel):
    execution_id: UUID
    origin: ExecutionOrigin
    status: AgentExecutionStatus
    started_at: datetime
    finished_at: datetime | None = None
    snapshot_id: UUID | None = None
    jobs_seen: int = 0
    jobs_selected: int = 0
    successful_submissions: int = 0
    error_count: int = 0
    last_error: str | None = None
