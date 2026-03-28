"""Domain layer for Job Applier."""

from job_applier.domain.entities import (
    ApplicationAnswer,
    ApplicationSubmission,
    ArtifactSnapshot,
    ExecutionEvent,
    JobPosting,
    ProfileSnapshot,
    RecruiterInteraction,
)
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
from job_applier.domain.versioning import Ruleset

__all__ = [
    "AnswerSource",
    "AgentExecutionStatus",
    "ApplicationAnswer",
    "ApplicationSubmission",
    "ArtifactSnapshot",
    "ArtifactType",
    "ExecutionEvent",
    "ExecutionEventType",
    "ExecutionOrigin",
    "FillStrategy",
    "JobPosting",
    "Platform",
    "ProfileSnapshot",
    "QuestionType",
    "RecruiterAction",
    "RecruiterInteraction",
    "RecruiterInteractionStatus",
    "Ruleset",
    "ScheduleFrequency",
    "SeniorityLevel",
    "SubmissionStatus",
    "WorkplaceType",
]
