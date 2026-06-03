"""In-memory store for successful submission audit records."""

from __future__ import annotations

from uuid import UUID

from job_applier.application.snapshotting import SuccessfulSubmissionRecord


class InMemorySuccessfulSubmissionStore:
    """Simple in-memory store for snapshot/ruleset lookup by submission."""

    def __init__(self) -> None:
        self._records: dict[UUID, SuccessfulSubmissionRecord] = {}

    def save(self, record: SuccessfulSubmissionRecord) -> None:
        """Persist a successful submission record in memory."""

        self._records[record.submission_id] = record

    def get(self, submission_id: UUID) -> SuccessfulSubmissionRecord | None:
        """Retrieve a persisted record by submission id."""

        return self._records.get(submission_id)
