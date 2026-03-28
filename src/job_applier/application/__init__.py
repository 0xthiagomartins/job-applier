"""Application layer for Job Applier."""

from job_applier.application.agent_execution import ExecutionRunSummary
from job_applier.application.config import UserAgentSettings
from job_applier.application.schemas import (
    AgentExecutionSummaryRead,
    ApplicationAnswerCreate,
    ApplicationAnswerRead,
    ApplicationSubmissionCreate,
    ApplicationSubmissionRead,
    ArtifactSnapshotCreate,
    ArtifactSnapshotRead,
    ExecutionEventCreate,
    ExecutionEventRead,
    JobPostingCreate,
    JobPostingRead,
    ProfileSnapshotCreate,
    ProfileSnapshotRead,
    RecruiterInteractionCreate,
    RecruiterInteractionRead,
    UserAgentConfigRead,
    UserAgentConfigWrite,
)
from job_applier.application.snapshotting import (
    SuccessfulSubmissionRecord,
    build_profile_snapshot,
    create_successful_submission_record,
)

__all__ = [
    "ApplicationAnswerCreate",
    "ApplicationAnswerRead",
    "AgentExecutionSummaryRead",
    "ApplicationSubmissionCreate",
    "ApplicationSubmissionRead",
    "ArtifactSnapshotCreate",
    "ArtifactSnapshotRead",
    "ExecutionEventCreate",
    "ExecutionEventRead",
    "JobPostingCreate",
    "JobPostingRead",
    "ProfileSnapshotCreate",
    "ProfileSnapshotRead",
    "RecruiterInteractionCreate",
    "RecruiterInteractionRead",
    "SuccessfulSubmissionRecord",
    "UserAgentConfigRead",
    "UserAgentConfigWrite",
    "UserAgentSettings",
    "ExecutionRunSummary",
    "build_profile_snapshot",
    "create_successful_submission_record",
]
