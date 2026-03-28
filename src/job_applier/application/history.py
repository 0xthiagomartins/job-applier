"""History query models used by persistence adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from job_applier.domain.entities import (
    ApplicationAnswer,
    ApplicationSubmission,
    ArtifactSnapshot,
    ExecutionEvent,
    JobPosting,
    ProfileSnapshot,
    RecruiterInteraction,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmissionHistoryFilters:
    """Filters supported by the successful submission history query."""

    company_name: str | None = None
    title: str | None = None
    external_job_id: str | None = None
    submitted_from: datetime | None = None
    submitted_to: datetime | None = None
    limit: int = 20
    offset: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmissionHistoryEntry:
    """Historical view of one successful application."""

    submission: ApplicationSubmission
    job_posting: JobPosting
    answers: tuple[ApplicationAnswer, ...]
    profile_snapshot: ProfileSnapshot | None
    recruiter_interactions: tuple[RecruiterInteraction, ...]
    execution_events: tuple[ExecutionEvent, ...]
    artifacts: tuple[ArtifactSnapshot, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmissionHistoryPage:
    """Paginated response returned by history queries."""

    items: tuple[SubmissionHistoryEntry, ...]
    total: int
    limit: int
    offset: int
