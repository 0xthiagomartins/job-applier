"""Application layer for Job Applier."""

from job_applier.application.config import UserAgentSettings
from job_applier.application.schemas import (
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
    "build_profile_snapshot",
    "create_successful_submission_record",
]
