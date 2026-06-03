"""Application layer for Job Applier."""

from __future__ import annotations

from pkgutil import extend_path
from typing import TYPE_CHECKING, Any

__path__ = extend_path(__path__, __name__)

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

if TYPE_CHECKING:
    from job_applier.application.agent_execution import ExecutionRunSummary

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


def __getattr__(name: str) -> Any:
    if name == "ExecutionRunSummary":
        from job_applier.application.agent_execution import ExecutionRunSummary

        return ExecutionRunSummary
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
