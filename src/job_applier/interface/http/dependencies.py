"""Shared FastAPI dependencies."""

from functools import lru_cache

from job_applier.application.agent_execution import AgentExecutionOrchestrator
from job_applier.application.agent_scheduler import AgentScheduler
from job_applier.infrastructure import (
    InMemorySuccessfulSubmissionStore,
    LocalExecutionStore,
    LocalPanelSettingsStore,
)
from job_applier.settings import get_runtime_settings


@lru_cache(maxsize=1)
def get_panel_settings_store() -> LocalPanelSettingsStore:
    """Return the panel settings store singleton."""

    settings = get_runtime_settings()
    return LocalPanelSettingsStore(root_dir=settings.resolved_panel_storage_dir)


@lru_cache(maxsize=1)
def get_execution_store() -> LocalExecutionStore:
    """Return the local execution store singleton."""

    settings = get_runtime_settings()
    return LocalExecutionStore(root_dir=settings.data_dir / "executions")


@lru_cache(maxsize=1)
def get_successful_submission_store() -> InMemorySuccessfulSubmissionStore:
    """Return the in-memory successful submission store singleton."""

    return InMemorySuccessfulSubmissionStore()


@lru_cache(maxsize=1)
def get_agent_orchestrator() -> AgentExecutionOrchestrator:
    """Return the execution orchestrator singleton."""

    return AgentExecutionOrchestrator(
        panel_store=get_panel_settings_store(),
        execution_store=get_execution_store(),
        successful_submission_store=get_successful_submission_store(),
    )


@lru_cache(maxsize=1)
def get_agent_scheduler() -> AgentScheduler:
    """Return the scheduler singleton."""

    settings = get_runtime_settings()
    return AgentScheduler(
        panel_store=get_panel_settings_store(),
        orchestrator=get_agent_orchestrator(),
        poll_interval_seconds=settings.scheduler_poll_interval_seconds,
    )
