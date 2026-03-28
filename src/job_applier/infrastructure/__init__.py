"""Infrastructure layer for Job Applier."""

from job_applier.infrastructure.in_memory.audit_store import InMemorySuccessfulSubmissionStore

__all__ = ["InMemorySuccessfulSubmissionStore"]
