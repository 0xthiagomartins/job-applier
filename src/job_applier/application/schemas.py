"""Pydantic schemas used for API and internal flow validation."""

from __future__ import annotations

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


class UserProfileConfigSchema(BaseModel):
    name: str
    email: EmailStr
    phone: str
    city: str
    linkedin_url: AnyUrl
    github_url: AnyUrl | None = None
    portfolio_url: AnyUrl | None = None
    years_experience_by_stack: dict[str, int] = Field(default_factory=dict)
    work_authorized: bool
    needs_sponsorship: bool = False
    salary_expectation: int | None = None
    availability: str
    default_responses: dict[str, str] = Field(default_factory=dict)
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
    api_key: SecretStr


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
