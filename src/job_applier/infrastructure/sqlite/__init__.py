"""SQLite persistence adapters."""

from job_applier.infrastructure.sqlite.database import (
    Base,
    create_session_factory,
    create_sqlalchemy_engine,
)
from job_applier.infrastructure.sqlite.repositories import (
    SqliteAnswerRepository,
    SqliteArtifactSnapshotRepository,
    SqliteExecutionEventRepository,
    SqliteJobPostingRepository,
    SqliteProfileSnapshotRepository,
    SqliteRecruiterInteractionRepository,
    SqliteSubmissionHistoryRepository,
    SqliteSubmissionRepository,
    build_sqlite_repositories,
)

__all__ = [
    "Base",
    "SqliteAnswerRepository",
    "SqliteArtifactSnapshotRepository",
    "SqliteExecutionEventRepository",
    "SqliteJobPostingRepository",
    "SqliteProfileSnapshotRepository",
    "SqliteRecruiterInteractionRepository",
    "SqliteSubmissionHistoryRepository",
    "SqliteSubmissionRepository",
    "build_sqlite_repositories",
    "create_session_factory",
    "create_sqlalchemy_engine",
]
