"""Infrastructure layer for Job Applier."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from job_applier.infrastructure.in_memory.audit_store import (
        InMemorySuccessfulSubmissionStore,
    )
    from job_applier.infrastructure.local_execution_store import (
        LocalExecutionStore,
        MirroredExecutionStore,
    )
    from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore

__all__ = [
    "InMemorySuccessfulSubmissionStore",
    "LocalExecutionStore",
    "LocalPanelSettingsStore",
    "MirroredExecutionStore",
]


def __getattr__(name: str) -> Any:
    """Load infrastructure exports lazily to avoid import cycles."""

    if name == "InMemorySuccessfulSubmissionStore":
        from job_applier.infrastructure.in_memory.audit_store import (
            InMemorySuccessfulSubmissionStore,
        )

        return InMemorySuccessfulSubmissionStore
    if name == "LocalExecutionStore":
        from job_applier.infrastructure.local_execution_store import LocalExecutionStore

        return LocalExecutionStore
    if name == "MirroredExecutionStore":
        from job_applier.infrastructure.local_execution_store import MirroredExecutionStore

        return MirroredExecutionStore
    if name == "LocalPanelSettingsStore":
        from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore

        return LocalPanelSettingsStore

    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
