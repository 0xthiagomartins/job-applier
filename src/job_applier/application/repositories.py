"""Repository protocols used by persistence adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, TypeVar
from uuid import UUID

from job_applier.application.history import (
    SubmissionHistoryEntry,
    SubmissionHistoryFilters,
    SubmissionHistoryPage,
)
from job_applier.domain.entities import (
    ApplicationAnswer,
    ApplicationSubmission,
    ArtifactSnapshot,
    ExecutionEvent,
    JobPosting,
    ProfileSnapshot,
    RecruiterInteraction,
)

EntityT = TypeVar("EntityT")


class Repository(Protocol[EntityT]):
    """Base CRUD-like repository contract."""

    def save(self, entity: EntityT) -> EntityT:
        """Persist a new or updated entity."""

    def get(self, entity_id: UUID) -> EntityT | None:
        """Load one entity by its identifier."""

    def list(self, *, limit: int = 100, offset: int = 0) -> list[EntityT]:
        """List persisted entities in a stable order."""

    def delete(self, entity_id: UUID) -> None:
        """Delete one entity when present."""


class JobPostingRepository(Repository[JobPosting], Protocol):
    """Persistence contract for job postings."""

    def find_by_external_job_id(
        self,
        *,
        platform: str,
        external_job_id: str,
    ) -> JobPosting | None:
        """Return one posting by platform and external identifier."""


class SubmissionRepository(Repository[ApplicationSubmission], Protocol):
    """Persistence contract for submissions."""

    def list_by_submitted_at(
        self,
        *,
        submitted_from: datetime | None = None,
        submitted_to: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ApplicationSubmission]:
        """List submissions filtered by submitted timestamp."""


class AnswerRepository(Repository[ApplicationAnswer], Protocol):
    """Persistence contract for application answers."""

    def list_for_submission(self, submission_id: UUID) -> list[ApplicationAnswer]:
        """List answers linked to one submission."""


class ProfileSnapshotRepository(Repository[ProfileSnapshot], Protocol):
    """Persistence contract for immutable profile snapshots."""


class RecruiterInteractionRepository(Repository[RecruiterInteraction], Protocol):
    """Persistence contract for recruiter interactions."""

    def list_for_submission(self, submission_id: UUID) -> list[RecruiterInteraction]:
        """List recruiter interactions linked to one submission."""


class ExecutionEventRepository(Repository[ExecutionEvent], Protocol):
    """Persistence contract for execution events."""

    def list_for_submission(self, submission_id: UUID) -> list[ExecutionEvent]:
        """List execution events linked to one submission."""

    def list_for_execution(self, execution_id: UUID) -> list[ExecutionEvent]:
        """List execution events linked to one execution."""


class ArtifactSnapshotRepository(Repository[ArtifactSnapshot], Protocol):
    """Persistence contract for execution artifacts."""

    def list_for_submission(self, submission_id: UUID) -> list[ArtifactSnapshot]:
        """List artifacts linked to one submission."""


class SubmissionHistoryRepository(Protocol):
    """Read model used by dashboards and audit views."""

    def query(self, filters: SubmissionHistoryFilters) -> SubmissionHistoryPage:
        """Return successful submissions filtered by business criteria."""

    def get(self, submission_id: UUID) -> SubmissionHistoryEntry | None:
        """Return one successful submission with full related audit context."""
